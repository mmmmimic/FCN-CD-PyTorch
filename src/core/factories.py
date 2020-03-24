from functools import wraps
from inspect import isfunction, isgeneratorfunction, getmembers
from collections.abc import Iterable
from itertools import chain
from importlib import import_module

import torch
import torch.nn as nn
import torch.utils.data as data

import constants
import utils.metrics as metrics
from utils.misc import R
from data.augmentation import *


class _Desc:
    def __init__(self, key):
        self.key = key
    def __get__(self, instance, owner):
        return tuple(getattr(instance[_],self.key) for _ in range(len(instance)))
    def __set__(self, instance, values):
        if not (isinstance(values, Iterable) and len(values)==len(instance)):
            raise TypeError("incorrect type or number of values")
        for i, v in zip(range(len(instance)), values):
            setattr(instance[i], self.key, v)


def _func_deco(func_name):
    def _wrapper(self, *args):
        return tuple(getattr(ins, func_name)(*args) for ins in self)
    return _wrapper


def _generator_deco(func_name):
    def _wrapper(self, *args, **kwargs):
        for ins in self:
            yield from getattr(ins, func_name)(*args, **kwargs)
    return _wrapper


# Duck typing
class Duck(tuple):
    __ducktype__ = object
    def __new__(cls, *args):
        if any(not isinstance(a, cls.__ducktype__) for a in args):
            raise TypeError("please check the input type")
        return tuple.__new__(cls, args)

    def __add__(self, tup):
        raise NotImplementedError

    def __mul__(self, tup):
        raise NotImplementedError


class DuckMeta(type):
    def __new__(cls, name, bases, attrs):
        assert len(bases) == 1
        for k, v in getmembers(bases[0]):
            if k.startswith('__'):
                continue
            if isgeneratorfunction(v):
                attrs.setdefault(k, _generator_deco(k))
            elif isfunction(v):
                attrs.setdefault(k, _func_deco(k))
            else:
                attrs.setdefault(k, _Desc(k))
        attrs['__ducktype__'] = bases[0]
        return super().__new__(cls, name, (Duck,), attrs)


class DuckModel(nn.Module):
    def __init__(self, *models):
        super().__init__()
        ## XXX: The state_dict will be a little larger in size
        # Since some extra bytes are stored in every key
        self._m = nn.ModuleList(models)

    def __len__(self):
        return len(self._m)

    def __getitem__(self, idx):
        return self._m[idx]

    def __repr__(self):
        return repr(self._m)


class DuckOptimizer(torch.optim.Optimizer, metaclass=DuckMeta):
    # Cuz this is an instance method
    @property
    def param_groups(self):
        return list(chain.from_iterable(ins.param_groups for ins in self))

    # This is special in dispatching
    def load_state_dict(self, state_dicts):
        for optim, state_dict in zip(self, state_dicts):
            optim.load_state_dict(state_dict)


class DuckCriterion(nn.Module, metaclass=DuckMeta):
    pass


class DuckDataset(data.Dataset, metaclass=DuckMeta):
    pass


def _import_module(pkg: str, mod: str, rel=False):
    if not rel:
        # Use absolute import
        return import_module('.'.join([pkg, mod]), package=None)
    else:
        return import_module('.'+mod, package=pkg)


def single_model_factory(model_name, C):
    name = model_name.strip().upper()
    if name == 'SIAMUNET_CONC':
        from models.siamunet_conc import SiamUnet_conc
        return SiamUnet_conc(C.num_feats_in, 2)
    elif name == 'SIAMUNET_DIFF':
        from models.siamunet_diff import SiamUnet_diff
        return SiamUnet_diff(C.num_feats_in, 2)
    elif name == 'EF':
        from models.unet import Unet
        return Unet(C.num_feats_in, 2)
    else:
        raise NotImplementedError("{} is not a supported architecture".format(model_name))


def single_optim_factory(optim_name, params, C):
    optim_name = optim_name.strip()
    name = optim_name.upper()
    if name == 'ADAM':
        return torch.optim.Adam(
            params, 
            betas=(0.9, 0.999),
            lr=C.lr,
            weight_decay=C.weight_decay
        )
    elif name == 'SGD':
        return torch.optim.SGD(
            params, 
            lr=C.lr,
            momentum=0.9,
            weight_decay=C.weight_decay
        )
    else:
        raise NotImplementedError("{} is not a supported optimizer type".format(optim_name))


def single_critn_factory(critn_name, C):
    import losses
    critn_name = critn_name.strip()
    try:
        criterion, params = {
            'L1': (nn.L1Loss, ()),
            'MSE': (nn.MSELoss, ()),
            'CE': (nn.CrossEntropyLoss, (torch.Tensor(C.weights),)),
            'NLL': (nn.NLLLoss, (torch.Tensor(C.weights),))
        }[critn_name.upper()]
        return criterion(*params)
    except KeyError:
        raise NotImplementedError("{} is not a supported criterion type".format(critn_name))


def _get_basic_configs(ds_name, C):
    if ds_name == 'OSCD':
        return dict(
            root = constants.IMDB_OSCD
        )
    elif ds_name.startswith('AC'):
        return dict(
            root = constants.IMDB_AIRCHANGE
        )
    elif ds_name.startswith('Lebedev'):
        return dict(
            root = constants.IMDB_LEBEDEV
        )
    else:
        return dict()
        

def single_train_ds_factory(ds_name, C):
    ds_name = ds_name.strip()
    module = _import_module('data', ds_name)
    dataset = getattr(module, ds_name+'Dataset')
    configs = dict(
        phase='train', 
        transforms=(Compose(Crop(C.crop_size), Flip()), None, None),
        repeats=C.repeats
    )
    
    # Update some common configurations
    configs.update(_get_basic_configs(ds_name, C))

    # Set phase-specific ones
    if ds_name == 'Lebedev':
        configs.update(
            dict(
                subsets = ('real',)
            )
        )
    else:
        pass
    
    dataset_obj = dataset(**configs)
    
    return data.DataLoader(
        dataset_obj,
        batch_size=C.batch_size,
        shuffle=True,
        num_workers=C.num_workers,
        pin_memory=not (C.device == 'cpu'), drop_last=True
    )


def single_val_ds_factory(ds_name, C):
    ds_name = ds_name.strip()
    module = _import_module('data', ds_name)
    dataset = getattr(module, ds_name+'Dataset')
    configs = dict(
        phase='val', 
        transforms=(None, None, None),
        repeats=1
    )

    # Update some common configurations
    configs.update(_get_basic_configs(ds_name, C))

    # Set phase-specific ones
    if ds_name == 'Lebedev':
        configs.update(
            dict(
                subsets = ('real',)
            )
        )
    else:
        pass
    
    dataset_obj = dataset(**configs)  

    # Create eval set
    return data.DataLoader(
        dataset_obj,
        batch_size=1,
        shuffle=False,
        num_workers=1,
        pin_memory=False, drop_last=False
    )


def _parse_input_names(name_str):
    return name_str.split('+')


def model_factory(model_names, C):
    name_list = _parse_input_names(model_names)
    if len(name_list) > 1:
        return DuckModel(*(single_model_factory(name, C) for name in name_list))
    else:
        return single_model_factory(model_names, C)


def optim_factory(optim_names, models, C):
    name_list = _parse_input_names(optim_names)
    num_models = len(models) if isinstance(models, DuckModel) else 1
    if len(name_list) != num_models:
        raise ValueError("the number of optimizers does not match the number of models")
    
    if num_models > 1:
        optims = []
        for name, model in zip(name_list, models):
            param_groups = [{'params': module.parameters(), 'name': module_name} for module_name, module in model.named_children()]
            optims.append(single_optim_factory(name, param_groups, C))
        return DuckOptimizer(*optims)
    else:
        return single_optim_factory(
            optim_names, 
            [{'params': module.parameters(), 'name': module_name} for module_name, module in models.named_children()], 
            C
        )


def critn_factory(critn_names, C):
    name_list = _parse_input_names(critn_names)
    if len(name_list) > 1:
        return DuckCriterion(*(single_critn_factory(name, C) for name in name_list))
    else:
        return single_critn_factory(critn_names, C)


def data_factory(dataset_names, phase, C):
    name_list = _parse_input_names(dataset_names)
    if phase not in ('train', 'val'):
        raise ValueError("phase should be either 'train' or 'val'")
    fact = globals()['single_'+phase+'_ds_factory']
    if len(name_list) > 1:
        return DuckDataset(*(fact(name, C) for name in name_list))
    else:
        return fact(dataset_names, C)


def metric_factory(metric_names, C):
    from utils import metrics
    name_list = _parse_input_names(metric_names)
    return [getattr(metrics, name.strip())() for name in name_list]
