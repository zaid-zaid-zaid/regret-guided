import os
import pickle
from typing import Any

import optax
import orbax.checkpoint
from orbax.checkpoint.utils import get_save_directory
from flax import core, struct
from flax.training import orbax_utils
from flax.training.train_state import TrainState
from jax import random as jr


class EMATrainState(TrainState):
    ema_params: core.FrozenDict[str, Any] = struct.field(pytree_node=True)


def new_train_state(rng_key, model, init_batch, config):
    params_key, sample_key = jr.split(rng_key)
    variables = model.init(
        {"params": params_key, "sample": sample_key},
        method="loss",
        inputs=init_batch,
        is_training=False,
    )
    if config.params.do_warmup and config.params.do_decay:
        lr = optax.warmup_cosine_decay_schedule(
            init_value=0.0,
            peak_value=config.params.learning_rate,
            warmup_steps=config.params.warmup_steps,
            decay_steps=config.params.decay_steps,
            end_value=config.params.end_learning_rate,
        )
    elif config.params.do_warmup:
        lr = optax.linear_schedule(
            init_value=0.0,
            end_value=config.params.learning_rate,
            transition_steps=config.params.warmup_steps,
        )
    elif config.params.do_decay:
        lr = optax.cosine_decay_schedule(
            init_value=config.params.learning_rate,
            decay_steps=config.params.decay_steps,
            alpha=config.params.end_learning_rate / config.params.learning_rate,
        )
    else:
        lr = config.params.learning_rate

    if config.name == "adamw":
        tx = optax.adamw(lr, weight_decay=config.params.weight_decay)
    else:
        tx = optax.adam(lr)

    if config.params.do_gradient_clipping:
        tx = optax.chain(
            optax.clip_by_global_norm(config.params.gradient_clipping), tx
        )

    return EMATrainState.create(
        apply_fn=model.apply,
        params=variables["params"],
        ema_params=variables["params"].copy(),
        tx=tx,
    )


def save_pickle(outfile, obj):
    with open(outfile, "wb") as handle:
        pickle.dump(obj, handle, protocol=pickle.HIGHEST_PROTOCOL)


def get_checkpointer_fns(outfolder, config, model_config):
    options = orbax.checkpoint.CheckpointManagerOptions(
        max_to_keep=config.max_to_keep,
        save_interval_steps=config.save_interval_steps,
        create=True,
        best_fn=lambda x: x["val_loss"],
        best_mode="min",
    )
    checkpointer = orbax.checkpoint.PyTreeCheckpointer()
    checkpoint_manager = orbax.checkpoint.CheckpointManager(
        outfolder,
        checkpointer,
        options,
    )
    save_pickle(os.path.join(outfolder, "config.pkl"), model_config)

    def save_fn(epoch, ckpt, metrics):
        save_args = orbax_utils.save_args_from_target(ckpt)
        checkpoint_manager.save(
            epoch, ckpt, save_kwargs={"save_args": save_args}, metrics=metrics
        )

    def restore_fn():
        return checkpoint_manager.restore(checkpoint_manager.best_step())

    def path_best_ckpt_fn():
        return get_save_directory(
            checkpoint_manager.best_step(), checkpoint_manager.directory
        )

    return save_fn, restore_fn, path_best_ckpt_fn
