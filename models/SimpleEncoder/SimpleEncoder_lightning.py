import os
import numpy as np
from scipy.interpolate import griddata
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau, StepLR
from torch.utils.data import DataLoader
import pytorch_lightning as pl
import wandb
import matplotlib.pyplot as plt

from models.SimpleEncoder.SimpleEncoder_pytorch import SimpleEncoder

# Define the pytorch lightning module for training the Simple Encoder model
class SimpleEncoderModule(pl.LightningModule):
    def __init__(self, input_dim=1, output_dim=1, d_model=32, nhead=8, num_layers=6,
                 domain_dim=1, # 1 for timeseries, 2 for spatial 2D
                 learning_rate=0.01, max_sequence_length=100,
                 do_layer_norm=True,
                 use_transformer=True,
                 use_positional_encoding='continuous',
                 append_position_to_x=False,
                 pos_enc_coeff=2,
                 include_y0_input=False,
                 activation='relu',
                 monitor_metric='train_loss',
                 lr_scheduler_params={'patience': 3,
                                      'factor': 0.5},
                 dropout=0.1, norm_first=False, dim_feedforward=2048):
        super(SimpleEncoderModule, self).__init__()
        self.first_forward = True # for plotting model-related things once at beginnning of training
        self.d_model = d_model
        self.learning_rate = learning_rate
        self.max_sequence_length = max_sequence_length
        self.use_transformer = use_transformer
        self.use_positional_encoding = use_positional_encoding
        self.append_position_to_x = append_position_to_x
        self.monitor_metric = monitor_metric
        self.lr_scheduler_params = lr_scheduler_params
        self.domain_dim = domain_dim

        # whether to use y as input to the encoder
        self.use_y_forward = include_y0_input

        # currently used for including v0 in the input to the encoder
        # can also be used for decoding later on

        self.model = SimpleEncoder(input_dim=input_dim,
                                    output_dim=output_dim,
                                    domain_dim=domain_dim,
                                    d_model=d_model, 
                                    nhead=nhead, 
                                    num_layers=num_layers,
                                    max_sequence_length=max_sequence_length,
                                    do_layer_norm=do_layer_norm,
                                    use_transformer=use_transformer,
                                    use_positional_encoding=use_positional_encoding,
                                    append_position_to_x=append_position_to_x,
                                    pos_enc_coeff=pos_enc_coeff,
                                    include_y0_input=include_y0_input,
                                    activation=activation,
                                    dropout=dropout,
                                    norm_first=norm_first,
                                    dim_feedforward=dim_feedforward)

    def forward(self, x, y, coords_x, coords_y):
        coords_x = coords_x[0].unsqueeze(2)
        coords_y = coords_y[0].unsqueeze(2)
        if self.first_forward:
            self.first_forward = False
            self.plot_positional_encoding(x, coords_x)

        if self.use_y_forward:
            return self.model(x, y=y, coords_x=coords_x)
        else: 
            return self.model(x, y=None, coords_x=coords_x)

    def training_step(self, batch, batch_idx):
        x, y, coords_x, coords_y = batch
        y_hat = self.forward(x, y, coords_x, coords_y)
        loss = F.mse_loss(y_hat, y)
        self.log("loss/train/mse", loss, on_step=False,
                 on_epoch=True, prog_bar=True)
        
        # Sup norm loss
        loss_sup  = torch.max(torch.abs(y_hat - y))
        self.log("loss/train/sup", loss_sup, on_step=False,
                 on_epoch=True, prog_bar=True)

        if batch_idx == 0:
            self.make_batch_figs(x, y, y_hat, coords_x, coords_y, tag='Train')

        return loss

    def on_after_backward(self):
        self.log_gradient_norms(tag='afterBackward')

    def on_before_optimizer_step(self, optimizer):
        # Compute the 2-norm for each layer and its gradient
        # If using mixed precision, the gradients are already unscaled here
        self.log_gradient_norms(tag='beforeOptimizer')
        self.log_parameter_norms(tag='beforeOptimizer')

    def log_gradient_norms(self, tag=''):
        norm_type = 2.0
        for name, param in self.named_parameters():
            if param.grad is not None:
                grad_norm = param.grad.detach().norm(norm_type)
                name = name.replace('.', '_')
                self.log(f"grad_norm/{tag}/{name}", grad_norm,
                         on_step=False, on_epoch=True, prog_bar=False)

    def log_parameter_norms(self, tag=''):
        norm_type = 2.0
        for name, param in self.named_parameters():
            param_norm = param.detach().norm(norm_type)
            name = name.replace('.', '_')
            self.log(f"param_norm/{tag}/{name}", param_norm,
                     on_step=False, on_epoch=True, prog_bar=False)

    def validation_step(self, batch, batch_idx):
        x, y, coords_x, coords_y = batch
        y_hat = self.forward(x, y, coords_x, coords_y)
        loss = F.mse_loss(y_hat, y)
        self.log("loss/val/mse", loss, on_step=False,
                 on_epoch=True, prog_bar=True)

        # Sup norm loss
        loss_sup  = torch.max(torch.abs(y_hat - y))
        self.log("loss/val/sup", loss_sup, on_step=False,
                 on_epoch=True, prog_bar=True)

        if batch_idx == 0:
            self.make_batch_figs(x, y, y_hat, coords_x, coords_y, tag='Val')
        return loss

    def plot_positional_encoding(self, x, coords):
        pe = self.model.positional_encoding(x, coords)
        plt.figure()
        fig, ax = plt.subplots(1, 1, figsize=(10, 6))
        plt.imshow(pe.detach().cpu().numpy(), cmap='viridis', aspect='auto')
        plt.colorbar()
        plt.title('Positional Encoding')
        wandb.log({f"plots/Positional Encoding": wandb.Image(fig)})
        plt.close()

    def make_batch_figs(self, x, y, y_hat, coords_x, coords_y, tag='', n_examples=5):
        if n_examples > x.shape[0]:
            n_examples = x.shape[0]
        idx = torch.arange(n_examples)
        y_pred = y_hat[idx].detach().cpu().numpy()
        y_true = y[idx].detach().cpu().numpy()
        coords_x = coords_x[idx].detach().cpu().numpy()
        coords_y = coords_y[idx].detach().cpu().numpy()

        if self.domain_dim == 1:
            self.batch_figs_1D(x, y_true, y_pred, coords_x, coords_y, tag, idx)
        elif self.domain_dim == 2:
            self.batch_figs_2D(x, y_true, y_pred, coords_x, coords_y, tag, idx)

    def batch_figs_1D(self, x, y_true, y_pred, coords_x, coords_y, tag, idx):
        # Plot Trajectories
        plt.figure()
        fig, axs = plt.subplots(
            nrows=y_true.shape[-1], ncols=len(idx), figsize=(10 * len(idx), 6 * y_true.shape[-1]), sharex=True, squeeze=False)

        for col, idx_val in enumerate(idx):
            for i, ax in enumerate(axs[:, col]):
                ax.plot(coords_y[idx_val], y_true[idx_val, :, i], linewidth=3,
                        color='blue', label='Ground Truth')
                ax.plot(coords_y[idx_val], y_pred[idx_val, :, i], linewidth=3,
                        color='red', label='Prediction')
                ax.set_xlabel('Time')
                ax.set_ylabel('Prediction')
                ax.set_title(
                    f'Trajectory for predicted component {i} (Index {idx_val})')
                if col == 0:
                    ax.legend()

        fig.suptitle(f'{tag} Trajectories: Prediction vs. Truth')
        plt.subplots_adjust(hspace=0.5)
        wandb.log(
            {f"plots/{tag}/Trajectories: Prediction vs. Truth": wandb.Image(fig)})
        plt.close()

        # compute value of each encoder layer sequentially
        # choose 3 random hidden dimensions to plot throughout
        idx_dim = [0, 1, 2]
        plt.figure()
        fig, axs = plt.subplots(
            nrows=len(idx_dim), ncols=len(idx), figsize=(10 * len(idx), 6 * len(idx_dim)), sharex=True)

        for col, idx_val in enumerate(idx):
            x_layer_output = self.model.linear_in(x[idx_val])
            for j, id in enumerate(idx_dim):
                axs[j, col].set_title(
                    f'Embedding dimension {id} over layer depth (Index {idx_val})')
                axs[j, col].plot(coords_x[idx_val],
                                x_layer_output.detach().cpu().numpy()[
                                    :, id].squeeze(),
                                linewidth=3, alpha=0.8, label='Layer {}'.format(0),
                                color=plt.cm.viridis(0))
            for i, layer in enumerate(self.model.encoder.layers):
                x_layer_output = layer(x_layer_output)
                # Plot the output of this layer
                for j, id in enumerate(idx_dim):
                    axs[j, col].plot(coords_x[idx_val],
                                    x_layer_output.detach().cpu().numpy()[
                                        :, id].squeeze(),
                                    linewidth=3, alpha=0.8, label=f'Layer {i+1}',
                                    color=plt.cm.viridis((i+1) / (len(self.model.encoder.layers))))

        axs[0, 0].legend()
        plt.subplots_adjust(hspace=0.5)
        fig.suptitle(f'{tag} Evolution of the Encoder Layers')
        wandb.log({f"plots/{tag}/Encoder Layer Plot": wandb.Image(fig)})
        plt.close('all')

    def batch_figs_2D(self, x, y_true, y_pred, coords_x, coords_y, tag, idx, n_grid=100):

        # Each element of y_true and y_pred is a 2D field with coordinates given by coords_y
        # plot the values of y_true and y_pred at the indices given by coords_y

        # Plot a 3 paneled figure with 3 scalar 2-d fields (heatmaps)
        # 1. Ground truth
        # 2. Prediction
        # 3. Relative difference

        # get the low and high indices of the y coordinates
        i_low_1, i_low_2 = np.min(coords_y[...,0]), np.min(coords_y[...,1])
        i_high_1, i_high_2 = np.max(coords_y[...,0]), np.max(coords_y[...,1])
        # build a meshgrid of coordinates based on coords_y
        y1i, y2i = np.meshgrid(np.linspace(i_low_1, i_high_1, n_grid), np.linspace(i_low_2, i_high_2, n_grid))

        # get the low and high indices of the x coordinates
        i_low_1, i_low_2 = np.min(coords_x[...,0]), np.min(coords_x[...,1])
        i_high_1, i_high_2 = np.max(coords_x[...,0]), np.max(coords_x[...,1])
        # build a meshgrid of coordinates based on coords_y
        x1i, x2i = np.meshgrid(np.linspace(i_low_1, i_high_1, n_grid), np.linspace(i_low_2, i_high_2, n_grid))

        plt.figure()
        fig, axs = plt.subplots(
            nrows=4, ncols=len(idx), figsize=(10 * len(idx), 6 * 4), sharex=True, squeeze=False)

        for col, idx_val in enumerate(idx):
            x_input_i = griddata(
                (coords_x[idx_val, :, 0], coords_x[idx_val, :, 1]), x[idx_val].detach().cpu().numpy(), (x1i, x2i), method='linear')
            y_true_i = griddata(
                (coords_y[idx_val, :, 0], coords_y[idx_val, :, 1]), y_true[idx_val], (y1i, y2i), method='linear')
            y_pred_i = griddata((coords_y[idx_val, :, 0], coords_y[idx_val, :, 1]), y_pred[idx_val], (y1i, y2i), method='linear')
            #y_true_i_norm = np.sqrt((1/(y_pred_i.shape[0]*y_pred_i.shape[1]))*np.sum(y_pred_i**2))
            y_rel_diff_i = np.abs(y_pred_i - y_true_i) / np.abs(y_true_i + 1e-5)
            #y_rel_diff_i = np.abs(y_pred_i - y_true_i)

            for i, ax in enumerate(axs[:, col]):
                if i == 0:
                    # plot input field x
                    im = ax.imshow(x_input_i, cmap='viridis')
                    ax.set_title(
                        f'Input Field (Index {idx_val})')

                if i == 1:
                    im = ax.imshow(y_true_i, cmap='viridis')
                    ax.set_title(
                        f'Ground Truth (Index {idx_val})')
                elif i == 2:
                    im = ax.imshow(y_pred_i, cmap='viridis')
                    ax.set_title(
                        f'Prediction (Index {idx_val})')
                elif i == 3:
                    # plot absolute relative error in log scale (difference divided by ground truth)
                    #im = ax.imshow(np.log10(y_rel_diff_i + 1e-10), cmap='viridis', vmin=-5, vmax=3)
                    im = ax.imshow(y_rel_diff_i, cmap='viridis')
                    ax.set_title(
                        f'Absolute Relative Error (Index {idx_val})')
                fig.colorbar(im, ax=ax)

        fig.suptitle(f'{tag} Predicted Fields: Prediction vs. Truth')
        plt.subplots_adjust(hspace=0.5)
        wandb.log(
            {f"plots/{tag}/Predicted Fields: Prediction vs. Truth": wandb.Image(fig)})
        plt.close()

    def test_step(self, batch, batch_idx, dataloader_idx=0):

        dt = self.trainer.datamodule.test_sample_rates[dataloader_idx]

        x, y, coords_x, coords_y = batch
        y_hat = self.forward(x, y, coords_x, coords_y)
        loss = F.mse_loss(y_hat, y)
        self.log(f"loss/test/mse/dt{dt}", loss, on_step=False,
                 on_epoch=True, prog_bar=True)
        
        # Sup norm loss
        loss_sup  = torch.max(torch.abs(y_hat - y))
        self.log(f"loss/test/sup/dt{dt}", loss_sup, on_step=False,
                 on_epoch=True, prog_bar=True)

        # log plots
        if batch_idx == 0:
            self.make_batch_figs(x, y, y_hat, coords_x, coords_y, tag=f'Test/dt{dt}')
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        config = {
            # REQUIRED: The scheduler instance
            "scheduler": ReduceLROnPlateau(optimizer, verbose=True, **self.lr_scheduler_params),
            # The unit of the scheduler's step size, could also be 'step'.
            # 'epoch' updates the scheduler on epoch end whereas 'step'
            # updates it after a optimizer update.
            "interval": "epoch",
            # How many epochs/steps should pass between calls to
            # `scheduler.step()`. 1 corresponds to updating the learning
            # rate after every epoch/step.
            "frequency": 1,
            # Metric to to monitor for schedulers like `ReduceLROnPlateau`
            "monitor": self.monitor_metric,  # "val_loss",
            # If set to `True`, will enforce that the value specified 'monitor'
            # is available when the scheduler is updated, thus stopping
            # training if not found. If set to `False`, it will only produce a warning
            "strict": True,
            # If using the `LearningRateMonitor` callback to monitor the
            # learning rate progress, this keyword can be used to specify
            # a custom logged name
            "name": None,
        }

        return {
            "optimizer": optimizer,
            "lr_scheduler": config,
        }
