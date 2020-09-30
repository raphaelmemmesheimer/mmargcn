import os
import time
import locale

locale.setlocale(locale.LC_ALL, "")

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.cuda.amp import autocast, GradScaler
import ray
from ray import tune

from util.graph import Graph
from util.dynamic_import import import_model, import_dataset_constants

from config import get_configuration, load_and_merge_configuration, save_configuration
from progress import ProgressLogger, CheckpointManager
from metrics import MultiClassAccuracy, TopKAccuracy, MetricsContainer, SimpleMetric
from data_input import SkeletonDataset
import torch_util
import session_helper


class Session:
    """
    Class for training and evaluating a model + logging
    """

    def __init__(self, config):
        self._config = config
        if self._config.session_id is not None:
            self._session_id = self._config.session_id
            self.is_resume = True
        else:
            self.is_resume = False
            self._session_type = self._config.session_type or "training"
            id_fmt = f"{self._session_type}_%Y_%m_%d-%H_%M_%S_torch{torch.version.__version__}"
            self._session_id = time.strftime(id_fmt)
            if self._config.debug:
                self._session_id = "debug_" + self._session_id

        self.log_path = os.path.join(self._config.out_path, self._session_id, "logs")
        self.checkpoint_path = os.path.join(self._config.out_path, self._session_id, "checkpoints")
        self.config_path = os.path.join(self._config.out_path, self._session_id, "config.yaml")

        if self.is_resume:
            load_and_merge_configuration(self._config, self.config_path)

        if self._config.batch_size == self._config.grad_accum_step:
            self._batch_fun = self._single_batch
            self._gradient_accumulation_steps = 0
        else:
            self._batch_fun = self._single_batch_accum
            self._gradient_accumulation_steps = self._config.batch_size // self._config.grad_accum_step
        if self._config.mixed_precision:
            # https://pytorch.org/docs/stable/amp.html
            # https://pytorch.org/docs/stable/notes/amp_examples.html#amp-examples
            self._forward = self._amp_forward
            self._loss_scale = GradScaler()
        else:
            self._forward = self._default_forward
            self._loss_scale = None
        self._model = None
        self._dataset_constants = {}
        self._loss_function = None
        self._optimizer = None
        self._lr_scheduler = None
        self._starting_epoch = 0
        self._checkpoint_manager = None
        self._data_loader = {}
        self._num_training_batches = None
        self._num_validation_batches = None
        self._metrics = None
        self._progress = None
        self._reporter = None

    def _initialize_model(self, model_config):
        # TODO implement tuning: https://docs.ray.io/en/latest/tune/index.html
        # https://stackoverflow.com/questions/44260217/hyperparameter-optimization-for-pytorch-model
        skeleton_edges, data_shape, num_classes = import_dataset_constants(self._config.dataset, [
            "skeleton_edges", "data_shape", "num_classes"
        ])

        graph = Graph(skeleton_edges)
        # https://pytorch.org/docs/stable/generated/torch.nn.Module.html
        # noinspection PyPep8Naming
        Model = import_model(self._config.model)
        self._model = Model(data_shape, num_classes, graph).cuda()
        self._loss_function = nn.CrossEntropyLoss().cuda()
        self._optimizer = session_helper.create_optimizer(model_config["optimizer"], self._model,
                                                          model_config["base_lr"],
                                                          **model_config["optimizer_args"])

        if self._config.lr_scheduler == "onecycle":
            model_config["epochs"] = self._config.epochs
            model_config["steps_per_epoch"] = self._num_training_batches

        self._lr_scheduler = session_helper.create_learning_rate_scheduler(model_config["lr_scheduler"],
                                                                           self._optimizer,
                                                                           **model_config["lr_scheduler_args"])

        state_dict_objects = {
            "model": self._model,
            "optimizer": self._optimizer,
            "loss_function": self._loss_function,
        }

        if self._lr_scheduler:
            state_dict_objects["lr_scheduler"] = self._lr_scheduler

        if self._loss_scale:
            state_dict_objects["loss_scale"] = self._loss_scale

        self._checkpoint_manager = CheckpointManager(self.checkpoint_path, state_dict_objects)

        if self.is_resume:
            cp = self._checkpoint_manager.load_latest()
            self._starting_epoch = cp["epoch"] + 1

    def start(self, config=None, reporter=None):
        """
        Start training or validation based on session type.
        """
        # if config is None:
        #     config = training_helper.get_model_config(self._config)
        self._initialize_model(config)
        self._load_data()
        if self._config.tuning:
            print("Config:", config)
        else:
            self.print_summary(config)
        self._reporter = reporter

        os.makedirs(self.log_path, exist_ok=True)
        # Start either training or only validation (requires pretrained model)
        log_file = None
        if self._config.tuning:
            log_file = open(os.path.join(self.log_path, "progress.log"), "w")
        self._build_metrics(log_file)
        save_configuration(self._config, self.config_path)
        self._progress.begin_session(self._session_type)
        if self._session_type == "training":
            self._start_training()
        elif self._session_type == "validation":
            self._start_validation()
        else:
            raise ValueError("Unknown session type " + str(self._session_type))
        self._progress.end_session()
        if log_file:
            log_file.close()

    def print_summary(self, config):
        """
        Print session and model related information before training/evaluation starts.
        """
        num_trainable_params = sum(p.numel() for p in self._model.parameters() if p.requires_grad)
        num_total_params = sum(p.numel() for p in self._model.parameters())
        print("PyTorch", torch.version.__version__, "CUDA", torch.version.cuda)
        print("Session ID:", self._session_id)
        print("Session Type:", self._session_type)
        if self._config.fixed_seed is not None:
            print("Fixed seed:", self._config.fixed_seed)
        print("Model:", self._config.model.upper())
        print("Dataset:", self._config.dataset.replace("_", "-").upper())
        print(f"Model - Trainable parameters: {num_trainable_params:n} | Total parameters: {num_total_params:n}")
        print("Batch size:", self._config.batch_size)
        print("Gradient accumulation step size:", self._config.grad_accum_step)
        print("Test batch size:", self._config.test_batch_size)
        print("Mixed precision:", self._config.mixed_precision)
        if self._session_type == "training":
            print(f"Training batches: {self._num_training_batches:n}")
        print(f"Evaluation batches: {self._num_validation_batches:n}")
        print("Logs will be written to:", self.log_path)
        print("Model checkpoints will be written to:", self.checkpoint_path)
        print("Config:", config)

    def _build_metrics(self, log_file=None):
        """
        Create the logger and metrics container to measure performance,
        accumulate metrics and print them to console and tensorboard.
        """
        self._progress = ProgressLogger(self.log_path, self._config.epochs, modes=[
            ("training", self._num_training_batches),
            ("validation", self._num_validation_batches)
        ], file=log_file)
        # TODO add multi-class precision and recall
        # https://medium.com/data-science-in-your-pocket/calculating-precision-recall-for-multi-class-classification-9055931ee229
        # https://towardsdatascience.com/multi-class-metrics-made-simple-part-i-precision-and-recall-9250280bddc2?gi=a28f7efba99e
        self._metrics = MetricsContainer([
            MultiClassAccuracy("training_accuracy"),
            MultiClassAccuracy("validation_accuracy"),
            TopKAccuracy("training_top5_accuracy"),
            TopKAccuracy("validation_top5_accuracy"),
            SimpleMetric("lr")
        ], related_metrics={
            "loss": ["lr", "training_loss", "validation_loss"],
            "accuracy": ["lr", "training_accuracy", "validation_accuracy"],
            "top5_accuracy": ["lr", "training_top5_accuracy", "validation_top5_accuracy"]
        })

    def _load_data(self):
        """
        Load validation and training data (if session type is training).
        """
        if self._session_type == "training":
            shuffle = not (self._config.disable_shuffle or self._config.debug)
            self._data_loader["train"] = DataLoader(
                SkeletonDataset(self._config.training_features_path, self._config.training_labels_path,
                                self._config.debug), self._config.batch_size, shuffle=shuffle,
                drop_last=True, worker_init_fn=torch_util.set_seed if self._config.fixed_seed is not None else None)
            self._num_training_batches = len(self._data_loader["train"])

        self._data_loader["val"] = DataLoader(
            SkeletonDataset(self._config.validation_features_path, self._config.validation_labels_path),
            self._config.test_batch_size, shuffle=False, drop_last=False,
            worker_init_fn=torch_util.set_seed if self._config.fixed_seed is not None else None)
        self._num_validation_batches = len(self._data_loader["val"])

    def _default_forward(self, features: torch.Tensor, label: torch.Tensor, loss_quotient: int = 1):
        y_pred = self._model(features)
        loss = self._loss_function(y_pred, label) / loss_quotient
        return y_pred, loss

    def _amp_forward(self, features: torch.Tensor, label: torch.Tensor, loss_quotient: int = 1):
        with autocast():
            return self._default_forward(features, label, loss_quotient)

    def _backward(self, loss: torch.Tensor):
        if self._config.mixed_precision:
            self._loss_scale.scale(loss).backward()
        else:
            loss.backward()

    def _single_batch(self, features: torch.Tensor, label: torch.Tensor):
        """
        Compute and calculate the loss for a single batch. If training, propagate the loss to all parameters.
        :param features: features tensor of len batch_size
        :param label: label tensor of len batch_size
        """
        y_pred, loss = self._forward(features, label)

        if self._model.training:
            self._backward(loss)
            # update online mean loss and metrics
            update_metrics = self._metrics.update_training
        else:
            update_metrics = self._metrics.update_validation

        update_metrics(loss, (y_pred, label), len(label))

    def _single_batch_accum(self, features: torch.Tensor, label: torch.Tensor):
        """
        Compute and calculate the loss for a single batch in small steps using gradient accumulation.
        If training, propagate the loss to all parameters.
        :param features: features tensor of len batch_size
        :param label: label tensor of len batch_size
        """
        for step in range(self._gradient_accumulation_steps):
            start = step * self._config.grad_accum_step
            end = start + self._config.grad_accum_step
            x, y_true = features[start:end], label[start:end]
            y_pred, loss = self._forward(x, y_true, len(y_true))

            if self._model.training:
                self._backward(loss)

                # update online mean loss and metrics
                update_metrics = self._metrics.update_training
            else:
                update_metrics = self._metrics.update_validation

            update_metrics(loss, (y_pred, y_true), len(y_true))

    def _train_epoch(self):
        """
        Train a single epoch by running over all training batches.
        """
        self._model.train()

        for features_batch, label_batch, indices in self._data_loader["train"]:
            with torch.no_grad():
                features = features_batch.float().cuda()
                label = label_batch.long().cuda()

            # Clear gradients for each parameter
            self._optimizer.zero_grad()
            # Compute model and calculate loss
            self._batch_fun(features, label)
            # Update weights
            if self._config.mixed_precision:
                self._loss_scale.step(self._optimizer)
                self._loss_scale.update()
            else:
                self._optimizer.step()
            # Update progress bar
            self._progress.update_epoch_mode(0, metrics=self._metrics.format_training())

    def _validate_epoch(self):
        """
        Validate a single epoch by running over all validation batches.
        """
        self._model.eval()
        mode = 1 if self._session_type == "training" else 0
        with torch.no_grad():
            for features_batch, label_batch, indices in self._data_loader["val"]:
                features = features_batch.float().cuda()
                label = label_batch.long().cuda()
                self._batch_fun(features, label)
                # Update progress bar
                self._progress.update_epoch_mode(mode, metrics=self._metrics.format_all())

    def _start_training(self):
        # TODO When using Gradient Accumulation: Replace BatchNorm layers with GroupNorm layers
        # https://discuss.pytorch.org/t/proper-way-of-fixing-batchnorm-layers-during-training/13214/3
        # https://medium.com/analytics-vidhya/effect-of-batch-size-on-training-process-and-results-by-gradient-accumulation-e7252ee2cb3f
        # https://towardsdatascience.com/what-is-gradient-accumulation-in-deep-learning-ec034122cfa?gi=ac2bf65a793c

        for epoch in range(self._config.epochs):
            # Begin epoch
            self._metrics["lr"].update(self._optimizer.param_groups[0]["lr"])
            self._progress.begin_epoch(epoch)

            # Training for current epoch
            self._progress.begin_epoch_mode(0)
            self._train_epoch()

            # Validation for current epoch
            self._progress.begin_epoch_mode(1)
            self._validate_epoch()

            # Finalize epoch
            self._progress.end_epoch(self._metrics)
            val_loss = self._metrics["validation_loss"].value
            val_acc = self._metrics["validation_accuracy"].value
            if self._lr_scheduler:
                self._lr_scheduler.step()
            if self._reporter:
                self._reporter(mean_loss=val_loss, mean_accuracy=val_acc, epoch=epoch)
            else:
                self._checkpoint_manager.save_checkpoint(epoch, val_acc)
            self._metrics.reset_all()

        self._checkpoint_manager.save_weights(self._model, self._session_id)

    def _start_validation(self):
        # TODO implement validation only
        pass


if __name__ == "__main__":
    cf = get_configuration()
    if cf.fixed_seed is not None:
        torch_util.set_seed(cf.fixed_seed)

    session = Session(cf)
    if cf.tuning:
        ray.init(include_dashboard=False, local_mode=True, num_gpus=1, num_cpus=1)
        analysis = tune.run(session.start, "Session", config=training_helper.get_tune_config())
        result = analysis.get_best_trial("mean_accuracy")
        print("Best trial config: {}".format(result.config))
        print("Best trial final validation loss: {}".format(result.last_result["mean_loss"]))
        print("Best trial final validation accuracy: {}".format(result.last_result["mean_accuracy"]))
    else:
        session.start()
