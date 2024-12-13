"""
Main training agent

"""

import logging
import os
import random

import einops
import numpy as np
import torch
from omegaconf import OmegaConf
from PIL import Image
from torch.utils.data import DataLoader
from transformers import AutoTokenizer

import wandb
from src.agent.dataset import TorchRLDSInterleavedDataset
from src.model.vla.model import VLA
from src.model.vla.processing import VLAProcessor
from src.utils.lr_scheduler import CosineAnnealingWarmupRestarts
from src.utils.monitor import Timer, log_allocated_gpu_memory, log_execution_time

log = logging.getLogger(__name__)


class TrainAgent:
    def __init__(self, cfg):
        # seeding
        self.seed = cfg.get("seed", 42)
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)

        # devices
        self.num_gpus = torch.cuda.device_count()
        self.gpu_id = int(cfg.gpu_id)
        self.multi_gpu = self.num_gpus > 1

        # training params
        self.n_epochs = cfg.n_epochs
        self.log_freq = cfg.log_freq
        self.save_model_batch_freq = cfg.save_model_batch_freq
        self.save_model_epoch_freq = cfg.save_model_epoch_freq
        self.logdir = cfg.logdir
        self.checkpoint_dir = os.path.join(self.logdir, "checkpoint")
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        self.use_wandb = cfg.get("wandb", None)
        if self.use_wandb and self.gpu_id == 0:
            wandb.init(
                entity=cfg.wandb.entity,
                project=cfg.wandb.project,
                name=cfg.wandb.run,
                config=OmegaConf.to_container(cfg, resolve=True),
            )
        self.debug = cfg.get("debug", False)

        # model
        self.model = VLA(cfg)
        if self.multi_gpu:
            log.info(f"Using {self.num_gpus} GPUs.")
            log.info(f"GPU for the current process: {self.gpu_id}")
            import torch.distributed as dist
            from torch.nn.parallel import DistributedDataParallel as DDP

            self.model = self.model.to(self.gpu_id)
            self.model = DDP(self.model, device_ids=[self.gpu_id])
            self.device = torch.device(f"cuda:{self.gpu_id}")
            dist.barrier()
        else:
            self.model = self.model.to(cfg.device)
            self.device = torch.device(cfg.device)
        if self.multi_gpu:
            model = self.model.module
        else:
            model = self.model
        if cfg.load_pretrained_weights:
            model.load_pretrained_weights()
        if self.multi_gpu:
            dist.barrier()
        log_allocated_gpu_memory(log, "loading model")

        # determine batch size and gradient accumulation steps
        self.gradient_accumulation_steps = (
            cfg.global_batch_size // cfg.per_device_batch_size // self.num_gpus
        )

        # tokenizer --- assume paligemma for now
        self.tokenizer = AutoTokenizer.from_pretrained(
            cfg.pretrained_model_path, padding_side="right"
        )
        assert self.tokenizer.padding_side == "right"

        # processor
        num_image_tokens = cfg.vision.config.num_image_tokens
        self.processor = VLAProcessor(self.tokenizer, num_image_tokens, cfg.max_seq_len)

        # dataloader --- use the same for all ranks
        dataset_wrapper = TorchRLDSInterleavedDataset(
            cfg.data,
            train=True,
        )
        self.dataloader = DataLoader(
            dataset_wrapper.dataset,
            batch_size=cfg.per_device_batch_size,
            num_workers=0,  # important to keep this to 0 so PyTorch does not mess with the parallelism
            shuffle=False,
            pin_memory=True,
            # sampler=DistributedSampler(dataset_wrapper.dataset),
        )
        num_batch_per_epoch = (
            len(dataset_wrapper.dataset) // cfg.per_device_batch_size // self.num_gpus
        )
        log.info(f"Global batch size: {cfg.global_batch_size}")
        log.info(f"Per device batch size: {cfg.per_device_batch_size}")
        log.info(f"Gradient accumulation steps: {self.gradient_accumulation_steps}")
        log.info(f"Number of batches per epoch: {num_batch_per_epoch}")

        # optimizer
        trained_parameters = (
            # list(mode.embed_tokens.parameters())
            list(model.vision_tower.parameters())
            + list(model.multi_modal_projector.parameters())
            # + list(model.joint_model.parameters())
            + list(model.action_time_encoder.parameters())
            + list(model.proprio_encoder.parameters())
            + list(model.action_decoder.parameters())
        )
        self.optimizer = torch.optim.AdamW(
            trained_parameters,
            lr=cfg.learning_rate,
            weight_decay=cfg.weight_decay,
        )
        self.lr_scheduler = CosineAnnealingWarmupRestarts(
            self.optimizer,
            first_cycle_steps=cfg.lr_scheduler.first_cycle_steps,
            cycle_mult=1.0,
            max_lr=cfg.learning_rate,
            min_lr=cfg.lr_scheduler.min_lr,
            warmup_steps=cfg.lr_scheduler.warmup_steps,
            gamma=1.0,
        )

    def run(self):
        timer = Timer()
        epoch = 1
        cnt_batch = 0
        cnt_update = 0
        if self.multi_gpu:
            import torch.distributed as dist

        for _ in range(self.n_epochs):
            log.info(f"Epoch {epoch}/{self.n_epochs} starts")
            for batch_ind, batch in enumerate(self.dataloader):
                cnt_update_in_epoch = 0
                """
                batch: dict with keys 'observation', 'task', 'action', 'dataset_name', 'action_pad_mask'
                observation: 'image_primary' (torch.Size([3, 1, 256, 256, 3], uint8), 'image_wrist', 'timestep' (torch.Size([3, 1])), 'pad_mask_dict', 'timestep_pad_mask', 'task_completed' (torch.Size([3, 1, 4]), 'proprio' (torch.Size([3, 1, 7])
                task: 'language_instruction', 'pad_mask_dict', 'image_primary', 'image_wrist', 'timestep' (torch.Size([3]))
                action (torch.Size([3, 1, 4, 7], float32)
                dataset_name
                action_pad_mask (torch.Size([3, 1, 4, 7]))
                """

                # extract and debug
                images = batch["observation"]["image_primary"]
                proprios = batch["observation"]["proprio"]
                actions = batch["action"].squeeze(1)  # remove the time dimension
                texts = [
                    text.decode("utf-8")
                    for text in batch["task"]["language_instruction"]
                ]
                if self.debug and batch_ind == 0:
                    log.info(f"{self.gpu_id} device {self.device}")
                    log.info(f"{self.gpu_id} texts {texts}")
                    log.info(f"{self.gpu_id} images {images.shape}")
                    log.info(
                        f"{self.gpu_id} actions {actions.shape} {actions.mean()} {actions.std()} {actions.max()} {actions.min()}"
                    )
                    log.info(
                        f"{self.gpu_id} proprios {proprios.shape} {proprios.mean()} {proprios.std()} {proprios.max()} {proprios.min()}"
                    )

                    # Save an image for debugging
                    image = images[0, 0].clone().to("cpu")
                    image = Image.fromarray(image.numpy())
                    image.save(os.path.join(self.logdir, f"image_{self.gpu_id}.png"))

                # process image and text
                images = einops.rearrange(
                    images, "B T H W C -> B (T C) H W"
                )  # remove cond_steps dimension
                model_inputs = self.processor(text=texts, images=images)
                pixel_values = model_inputs["pixel_values"]
                input_ids = model_inputs["input_ids"]
                attention_mask = model_inputs[
                    "attention_mask"
                ]  # with padding if bsz > 1

                # forward
                self.model.train()
                if self.multi_gpu:
                    model = self.model.module
                else:
                    model = self.model
                if self.debug:
                    log_allocated_gpu_memory(log, f"pre batch {batch_ind}")
                loss_train = model.loss(
                    pixel_values=pixel_values.to(self.device),
                    input_ids=input_ids.to(self.device),
                    proprios=proprios.to(self.device),
                    actions=actions.to(self.device),
                    attention_mask=attention_mask.to(self.device),
                )
                if self.debug:
                    log_allocated_gpu_memory(log, f"forward batch {batch_ind}")

                # update
                loss_train.backward()
                if self.debug:
                    log_allocated_gpu_memory(log, f"backward batch {batch_ind}")
                    self.optimizer.step()
                    if self.debug:
                        log_allocated_gpu_memory(
                            log, f"optimizer step batch {batch_ind}"
                        )
                    self.optimizer.zero_grad(set_to_none=True)
                    if self.debug:
                        log_allocated_gpu_memory(
                            log, f"optimizer zero grad batch {batch_ind}"
                        )
                    cnt_update += 1
                    cnt_update_in_epoch += 1

                # TODO: validation
                loss_val = None

                # clear cache
                torch.cuda.empty_cache()

                # log loss
                if self.gpu_id == 0 and cnt_batch % self.log_freq == 0:
                    log.info(
                        f"Epoch {epoch} Batch {batch_ind}: train loss {loss_train:8.4f} | t:{timer():8.4f}"
                    )
                    if self.use_wandb and self.gpu_id == 0:
                        wandb_metrics = {
                            "loss - train": loss_train,
                            "Gradient steps": cnt_update,
                        }
                        if loss_val is not None:
                            wandb_metrics["loss - val"] = loss_val
                        wandb.log(wandb_metrics, step=cnt_batch, commit=True)

                # update lr # TODO: ddp affects? if not synced
                if self.gpu_id == 0:
                    self.lr_scheduler.step()

                # Count
                cnt_batch += 1

                # save model at batch level
                if self.gpu_id == 0 and cnt_batch % self.save_model_batch_freq == 0:
                    self.save_model(epoch)

            # Save model at epoch level --- always save at the end
            if self.gpu_id == 0 and (
                epoch % self.save_model_epoch_freq == 0 or epoch == self.n_epochs
            ):
                self.save_model(epoch)

            # Count
            epoch += 1
            if self.multi_gpu:
                dist.barrier()

    @log_execution_time()
    def save_model(self, epoch):
        data = {
            "epoch": epoch,
            "model": (
                self.model.module.state_dict()
                if self.multi_gpu
                else self.model.state_dict()
            ),
        }
        savepath = os.path.join(self.checkpoint_dir, f"state_{epoch}.pt")
        torch.save(data, savepath)
        log.info(f"Saved model to {savepath}")

    def load_model(self, epoch):
        # TODO
        loadpath = os.path.join(self.checkpoint_dir, f"state_{epoch}.pt")
        data = torch.load(loadpath, weights_only=True)

        epoch = data["epoch"]
        if self.multi_gpu:
            model = self.model.module
        else:
            model = self.model
        model.load_state_dict(data["model"])
        return epoch
