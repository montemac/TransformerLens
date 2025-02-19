# Import stuff
import logging
from typing import Callable, Union, Optional, Sequence, Dict, List, Tuple
import torch
import torch.nn as nn
import torch.utils.hooks as hooks

from functools import *

from dataclasses import dataclass

@dataclass
class LensHandle:
    hook: hooks.RemovableHandle
    is_permanent: bool = False
    

# %%
# Define type aliases
NamesFilter = Optional[Union[Callable[[str], bool], Sequence[str]]]

# %%
# A helper class to get access to intermediate activations (inspired by Garcon)
# It's a dummy module that is the identity function by default
# I can wrap any intermediate activation in a HookPoint and get a convenient
# way to add PyTorch hooks
class HookPoint(nn.Module):
    def __init__(self):
        super().__init__()
        self.fwd_hooks: List[LensHandle] = []
        self.bwd_hooks: List[LensHandle] = []
        self.ctx = {}

        # A variable giving the hook's name (from the perspective of the root
        # module) - this is set by the root module at setup.
        self.name = None

    def add_perma_hook(self, hook, dir="fwd") -> None:
        self.add_hook(hook, dir=dir, is_permanent=True)

    def add_hook(self, hook, dir="fwd", is_permanent=False) -> None:
        # Hook format is fn(activation, hook_name)
        # Change it into PyTorch hook format (this includes input and output,
        # which are the same for a HookPoint)
        if dir == "fwd":

            def full_hook(module, module_input, module_output):
                return hook(module_output, hook=self)

            handle = self.register_forward_hook(full_hook)
            handle = LensHandle(handle, is_permanent)
            self.fwd_hooks.append(handle)
        elif dir == "bwd":
            # For a backwards hook, module_output is a tuple of (grad,) - I don't know why.
            def full_hook(module, module_input, module_output):
                return hook(module_output[0], hook=self)
            handle = self.register_full_backward_hook(full_hook)
            handle = LensHandle(handle, is_permanent)
            self.bwd_hooks.append(handle)
        else:
            raise ValueError(f"Invalid direction {dir}")

    def remove_hooks(self, dir="fwd", including_permanent=False) -> None:

        def _remove_hooks(handles: List[LensHandle]) -> List[LensHandle]:
            output_handles = []
            for handle in handles:
                if not handle.is_permanent or including_permanent:
                    handle.hook.remove()
                else:
                    output_handles.append(handle)
            return output_handles

        if dir == "fwd" or dir == "both":
            self.fwd_hooks = _remove_hooks(self.fwd_hooks)
        if dir == "bwd" or dir == "both":
            self.bwd_hooks = _remove_hooks(self.bwd_hooks)
        if dir not in ["fwd", "bwd", "both"]:
            raise ValueError(f"Invalid direction {dir}")

    def clear_context(self):
        del self.ctx
        self.ctx = {}

    def forward(self, x):
        return x

    def layer(self):
        # Returns the layer index if the name has the form 'blocks.{layer}.{...}'
        # Helper function that's mainly useful on HookedTransformer
        # If it doesn't have this form, raises an error -
        split_name = self.name.split(".")
        return int(split_name[1])


# %%
class HookedRootModule(nn.Module):
    """
    A class building on nn.Module to interface nicely with HookPoints
    Adds various nice utilities, most notably run_with_hooks to run the model with temporary hooks, and run_with_cache to run the model on some input and return a cache of all activations

    WARNING: The main footgun with PyTorch hooking is that hooks are GLOBAL state. If you add a hook to the module, and then run it a bunch of times, the hooks persist. If you debug a broken hook and add the fixed version, the broken one is still there. To solve this, run_with_hooks will remove hooks at the end by default, and I recommend using the API of this and run_with_cache. If you want to add hooks into global state, I recommend being intentional about this, and I recommend using reset_hooks liberally in your code to remove any accidentally remaining global state.

    The main time this goes wrong is when you want to use backward hooks (to cache or intervene on gradients). In this case, you need to keep the hooks around as global state until you've run loss.backward() (and so need to disable the reset_hooks_end flag on run_with_hooks)
    """

    def __init__(self, *args):
        super().__init__()
        self.is_caching = False

    def setup(self):
        # Setup function - this needs to be run in __init__ AFTER defining all
        # layers
        # Add a parameter to each module giving its name
        # Build a dictionary mapping a module name to the module
        self.mod_dict = {}
        self.hook_dict: Dict[str, HookPoint] = {}
        for name, module in self.named_modules():
            module.name = name
            self.mod_dict[name] = module
            if "HookPoint" in str(type(module)):
                self.hook_dict[name] = module

    def hook_points(self):
        return self.hook_dict.values()

    def remove_all_hook_fns(self, direction="both", including_permanent=False):
        for hp in self.hook_points():
            hp.remove_hooks(direction, including_permanent=including_permanent)

    def clear_contexts(self):
        for hp in self.hook_points():
            hp.clear_context()

    def reset_hooks(self, clear_contexts=True, direction="both", including_permanent=False):
        if clear_contexts:
            self.clear_contexts()
        self.remove_all_hook_fns(direction, including_permanent)
        self.is_caching = False

    def check_and_add_hook(self, hook_point, hook_point_name, hook, dir="fwd", is_permanent=False) -> None:
        """Override this function to add checks on which hooks should be added"""
        hook_point.add_hook(hook, dir=dir, is_permanent=is_permanent)

    def add_hook(self, name, hook, dir="fwd", is_permanent=False) -> None:
        if type(name) == str:
            self.check_and_add_hook(self.mod_dict[name], name, hook, dir=dir, is_permanent=is_permanent)
        else:
            # Otherwise, name is a Boolean function on names
            for hook_point_name, hp in self.hook_dict.items():
                if name(hook_point_name):
                    self.check_and_add_hook(hp, hook_point_name, hook, dir=dir, is_permanent=is_permanent)

    def add_perma_hook(self, name, hook, dir="fwd") -> None:
        self.add_hook(name, hook, dir=dir, is_permanent=True)

    def run_with_hooks(
        self,
        *model_args,
        fwd_hooks: List[Tuple[Union[str, Callable], Callable]] = [],
        bwd_hooks: List[Tuple[Union[str, Callable], Callable]] = [],
        reset_hooks_end=True,
        clear_contexts=False,
        **model_kwargs,
    ):
        """
        fwd_hooks: A list of (name, hook), where name is either the name of
        a hook point or a Boolean function on hook names and hook is the
        function to add to that hook point, or the hook whose names evaluate
        to True respectively. Ditto bwd_hooks
        reset_hooks_end (bool): If True, all hooks are removed at the end (ie,
        including those added in this run)
        clear_contexts (bool): If True, clears hook contexts whenever hooks are reset
        Note that if we want to use backward hooks, we need to set
        reset_hooks_end to be False, so the backward hooks are still there - this function only runs a forward pass.
        """
        try:
            for name, hook in fwd_hooks:
                if type(name) == str:
                    self.mod_dict[name].add_hook(hook, dir="fwd")
                else:
                    # Otherwise, name is a Boolean function on names
                    for hook_name, hp in self.hook_dict.items():
                        if name(hook_name):
                            hp.add_hook(hook, dir="fwd")
            for name, hook in bwd_hooks:
                if type(name) == str:
                    self.mod_dict[name].add_hook(hook, dir="bwd")
                else:
                    # Otherwise, name is a Boolean function on names
                    for hook_name, hp in self.hook_dict:
                        if name(hook_name):
                            hp.add_hook(hook, dir="bwd")
            return self.forward(*model_args, **model_kwargs)
        finally:
            if reset_hooks_end:
                if len(bwd_hooks) > 0:
                    logging.warning(
                        "WARNING: Hooks were reset at the end of run_with_hooks while backward hooks were set. This removes the backward hooks before a backward pass can occur"
                    )
                self.reset_hooks(clear_contexts, including_permanent=False)

    def add_caching_hooks(
        self,
        names_filter: NamesFilter = None,
        incl_bwd: bool = False,
        device=None,
        remove_batch_dim: bool = False,
        cache: Optional[dict] = None,
    ) -> dict:
        """Adds hooks to the model to cache activations. Note: It does NOT actually run the model to get activations, that must be done separately.

        Args:
            names_filter (NamesFilter, optional): Which activations to cache. Can be a list of strings (hook names) or a filter function mapping hook names to booleans. Defaults to lambda name: True.
            incl_bwd (bool, optional): Whether to also do backwards hooks. Defaults to False.
            device (_type_, optional): The device to store on. Defaults to model.cfg.device if set, else CUDA if available, else CPU.
            remove_batch_dim (bool, optional): Whether to remove the batch dimension (only works for batch_size==1). Defaults to False.
            cache (Optional[dict], optional): The cache to store activations in, a new dict is created by default. Defaults to None.

        Returns:
            cache (dict): The cache where activations will be stored.
        """
        if device is None:
            device = self.cfg.device
            if device is None:
                device = "cuda" if torch.cuda.is_available() else "cpu"
        if cache is None:
            cache = {}

        if names_filter is None:
            names_filter = lambda name: True
        elif type(names_filter) == str:
            names_filter = lambda name: name == names_filter
        elif type(names_filter) == list:
            names_filter = lambda name: name in names_filter

        self.is_caching = True

        def save_hook(tensor, hook):
            if remove_batch_dim:
                cache[hook.name] = tensor.detach().to(device)[0]
            else:
                cache[hook.name] = tensor.detach().to(device)

        def save_hook_back(tensor, hook):
            if remove_batch_dim:
                cache[hook.name + "_grad"] = tensor.detach().to(device)[0]
            else:
                cache[hook.name + "_grad"] = tensor.detach().to(device)

        for name, hp in self.hook_dict.items():
            if names_filter(name):
                hp.add_hook(save_hook, "fwd")
                if incl_bwd:
                    hp.add_hook(save_hook_back, "bwd")
        return cache

    def run_with_cache(
        self,
        *model_args,
        names_filter: NamesFilter = None,
        device=None,
        remove_batch_dim=False,
        incl_bwd=False,
        reset_hooks_end=True,
        clear_contexts=False,
        **model_kwargs,
    ):
        """
        Runs the model and returns model output and a Cache object

        model_args and model_kwargs - all positional arguments and keyword arguments not otherwise captured are input to the model
        names_filter (None or str or [str] or fn:str->bool): a filter for which activations to cache. Defaults to None, which means cache everything.
        device (str or torch.Device): The device to cache activations on, defaults to model device. Note that this must be set if the model does not have a model.cfg.device attribute. WARNING: Setting a different device than the one used by the model leads to significant performance degradation.
        remove_batch_dim (bool): If True, will remove the batch dimension when caching. Only makes sense with batch_size=1 inputs.
        incl_bwd (bool): If True, will call backward on the model output and also cache gradients. It is assumed that the model outputs a scalar, ie. return_type="loss", for predict the next token loss. Custom loss functions are not supported
        reset_hooks_end (bool): If True, all hooks are removed at the end (ie, both those added in this run *and* any added before!)
        clear_contexts (bool): If True, clears hook contexts whenever hooks are reset
        """
        cache_dict = self.add_caching_hooks(
            names_filter, incl_bwd, device, remove_batch_dim=remove_batch_dim
        )
        model_out = self(*model_args, **model_kwargs)

        if incl_bwd:
            model_out.backward()

        if reset_hooks_end:
            self.reset_hooks(clear_contexts, including_permanent=False)
        return model_out, cache_dict

    def cache_all(self, cache, incl_bwd=False, device=None, remove_batch_dim=False):
        logging.warning(
            "cache_all is deprecated and will eventually be removed, use add_caching_hooks or run_with_cache"
        )
        self.add_caching_hooks(
            names_filter=lambda name: True,
            cache=cache,
            incl_bwd=incl_bwd,
            device=device,
            remove_batch_dim=remove_batch_dim,
        )

    def cache_some(
        self,
        cache,
        names: Callable[[str], bool],
        incl_bwd=False,
        device=None,
        remove_batch_dim=False,
    ):
        """Cache a list of hook provided by names, Boolean function on names"""
        logging.warning(
            "cache_some is deprecated and will eventually be removed, use add_caching_hooks or run_with_cache"
        )
        self.add_caching_hooks(
            names_filter=names,
            cache=cache,
            incl_bwd=incl_bwd,
            device=device,
            remove_batch_dim=remove_batch_dim,
        )


# %%
