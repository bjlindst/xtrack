"""

Structure of the code:

MadLoader takes a sequence and several options
MadLooder.make_line(buffer=None) returns a line with elements installed in one buffer
MadLooder.iter_elements() iterates over the elements of the sequence,
                          yielding a MadElement and applies some simplifications

Developers:

- MadElem encapsulate a mad element, it behaves like an elemenent from the expanded sequence
but returns as attributes a value, or an expression if present.

- Use `if MadElem(mad).l: to check for no zero value and NOT `if MadElem(mad).l!=0:` because if l is an expression it will create the expression l!=0 and return True


- ElementBuilder, is a class that builds an xtrack element from a definition. If a values is expression, the value calculated from the expression, the expression if present is attached to the line.


Developer should write
Loader.convert_<name>(mad_elem)->List[ElementBuilder] to convert new element in a list

or in alternative

Loader.add_<name>(mad_elem,line,buffer) to add a new element to line

if the want to control how the xobject is created
"""
import abc
import functools
import re
from itertools import zip_longest
from typing import List, Iterable, Iterator, Tuple, Union

import numpy as np
from math import tan

import xtrack, xobjects
from .compounds import Compound

from .general import _print


# Generic functions

clight = 299792458

DEFAULT_BEND_N_MULT_KICKS = 5


def iterable(obj):
    return hasattr(obj, "__iter__")


def set_if_not_none(dct, key, val):
    if val is not None:
        dct[key] = val


def rad2deg(rad):
    return rad * 180 / np.pi


def get_value(x):
    if is_expr(x):
        return x._get_value()
    elif isinstance(x, list) or isinstance(x, tuple):
        return [get_value(xx) for xx in x]
    elif isinstance(x, np.ndarray):
        arr = np.zeros_like(x, dtype=float)
        for ii in np.ndindex(*x.shape):
            arr[ii] = get_value(x[ii])
    elif isinstance(x, dict):
        return {k: get_value(v) for k, v in x.items()}
    else:
        return x


def set_expr(target, key, xx):
    """
    Assumes target is either a struct supporting attr assignment or an array supporint item assignment.

    """
    if isinstance(xx, list):
        out = getattr(target, key)
        for ii, ex in enumerate(xx):
            set_expr(out, ii, ex)
    elif isinstance(xx, np.ndarray):
        out = getattr(target, key)
        for ii in np.ndindex(*xx.shape):
            set_expr(out, ii, xx[ii])
    elif isinstance(xx, dict):
        for kk, ex in xx.items():
            set_expr(target[key], kk, ex)
    elif xx is not None:
        if isinstance(key, int) or isinstance(key, tuple):
            target[key] = xx
        else:
            setattr(target, key, xx)  # issue if target is not a structure


# needed because cannot used += with numpy arrays of expressions
def add_lists(a, b, length):
    out = []
    for ii in range(length):
        if ii < len(a) and ii < len(b):
            c = a[ii] + b[ii]
        elif ii < len(a):
            c = a[ii]
        elif ii < len(b):
            c = b[ii]
        else:
            c = 0
        out.append(c)
    return out


def non_zero_len(lst):
    for ii, x in enumerate(lst[::-1]):
        if x:  # could be expression
            return len(lst) - ii
    return 0


def trim_trailing_zeros(lst):
    for ii in range(len(lst) - 1, 0, -1):
        if lst[ii] != 0:
            return lst[: ii + 1]
    return []


def is_expr(x):
    return hasattr(x, "_get_value")


def nonzero_or_expr(x):
    if is_expr(x):
        return True
    else:
        return x != 0


def value_if_expr(x):
    if is_expr(x):
        return x._value
    else:
        return x


def eval_list(par, madeval):
    if madeval is None:
        return par.value
    else:
        return [
            madeval(expr) if expr else value for value, expr in zip(par.value, par.expr)
        ]


def generate_repeated_name(line, name):
    if name in line.element_dict:
        ii = 0
        while f"{name}:{ii}" in line.element_dict:
            ii += 1
        return f"{name}:{ii}"
    else:
        return name


class FieldErrors:
    def __init__(self, field_errors):
        self.dkn = np.array(field_errors.dkn)
        self.dks = np.array(field_errors.dks)


class PhaseErrors:
    def __init__(self, phase_errors):
        self.dpn = np.array(phase_errors.dpn)
        self.dps = np.array(phase_errors.dps)


class MadElem:
    def __init__(self, name, elem, sequence, madeval=None, name_prefix=None):
        if name_prefix is None:
            self.name = name
        else:
            self.name = name_prefix + name
        self.elem = elem
        self.sequence = sequence
        self.madeval = madeval
        ### needed for merge multipoles
        if hasattr(elem, "field_errors") and elem.field_errors is not None:
            self.field_errors = FieldErrors(elem.field_errors)
        else:
            self.field_errors = None
        if elem.base_type.name != 'translation' and (
                elem.dphi or elem.dtheta or elem.dpsi
                or elem.dx or elem.dy or elem.ds):
            raise NotImplementedError

    # @property
    # def field_errors(self):
    #    elem=self.elem
    #    if hasattr(elem, "field_errors") and elem.field_errors is not None:
    #        return FieldErrors(elem.field_errors)

    def get_type_hierarchy(self, cpymad_elem=None):
        if cpymad_elem is None:
            cpymad_elem = self.elem

        if cpymad_elem.name == cpymad_elem.parent.name:
            return [cpymad_elem.name]

        parent_types = self.get_type_hierarchy(cpymad_elem.parent)
        return [cpymad_elem.name] + parent_types

    @property
    def phase_errors(self):
        elem = self.elem
        if hasattr(elem, "phase_errors") and elem.phase_errors is not None:
            return PhaseErrors(elem.phase_errors)

    @property
    def align_errors(self):
        elem = self.elem
        if hasattr(elem, "align_errors") and elem.align_errors is not None:
            return elem.align_errors

    def __repr__(self):
        return f"<{self.name}: {self.type}>"

    @property
    def type(self):
        return self.elem.base_type.name

    @property
    def slot_id(self):
        return self.elem.slot_id

    def __getattr__(self, k):
        par = self.elem.cmdpar.get(k)
        if par is None:
            raise AttributeError(
                f"Element `{self.name}: {self.type}` has no attribute `{k}`"
            )
        if isinstance(par.value, list):
            # return ParList(eval_list(par, self.madeval))
            return eval_list(par, self.madeval)
        elif isinstance(par.value, str):
            return par.value  # no need to make a Par for strings
        elif self.madeval is not None and par.expr is not None:
            return self.madeval(par.expr)
        else:
            return par.value

    def get(self, key, default=None):
        if hasattr(self, key):
            return getattr(self, key)
        else:
            return default

    def has_aperture(self):
        el = self.elem
        has_aper = hasattr(el, "aperture") and (
            el.aperture[0] != 0.0 or len(el.aperture) > 1
        )
        has_aper = has_aper or (hasattr(el, "aper_vx") and len(el.aper_vx) > 2)
        return has_aper

    def is_empty_marker(self):
        return self.type == "marker" and not self.has_aperture()

    def same_aperture(self, other):
        return (
            self.aperture == other.aperture
            and self.aper_offset == other.aper_offset
            and self.aper_tilt == other.aper_tilt
            and self.aper_vx == other.aper_vx
            and self.aper_vy == other.aper_vy
            and self.apertype == other.apertype
        )

    def merge_multipole(self, other):
        if (
            self.same_aperture(other)
            and self.align_errors == other.align_errors
            and self.tilt == other.tilt
            and self.angle == other.angle
        ):
            self.knl += other.knl
            self.ksl += other.ksl
            if self.field_errors is not None and other.field_errors is not None:
                for ii in range(len(self.field_errors.dkn)):
                    self.field_errors.dkn[ii] += other.field_errors.dkn[ii]
                    self.field_errors.dks[ii] += other.field_errors.dks[ii]
            self.name = self.name + "_" + other.name
            return True
        else:
            return False


class ElementBuilder:
    """
    init  is a dictionary of element data passed to the __init__ function of the element class
    attrs is a dictionary of extra data to be added to the element data after creation
    """

    def __init__(self, name, type, **attrs):
        self.name = name
        self.type = type
        self.attrs = {} if attrs is None else attrs

    def __repr__(self):
        return "Element(%s, %s, %s)" % (self.name, self.type, self.attrs)

    def __setattr__(self, k, v):
        if hasattr(self, "attrs") and k not in ('name', 'type', 'attrs'):
            self.attrs[k] = v
        else:
            super().__setattr__(k, v)

    def add_to_line(self, line, buffer):
        xtel = self.type(**self.attrs, _buffer=buffer)
        name = generate_repeated_name(line, self.name)
        line.append_element(xtel, name)


class ElementBuilderWithExpr(ElementBuilder):
    def add_to_line(self, line, buffer):
        attr_values = {k: get_value(v) for k, v in self.attrs.items()}
        xtel = self.type(**attr_values, _buffer=buffer)
        name = generate_repeated_name(line, self.name)
        line.append_element(xtel, name)
        elref = line.element_refs[name]
        for k, p in self.attrs.items():
            set_expr(elref, k, p)
        return xtel


class CompoundElementBuilder:
    """A builder-like object for holding elements that should become a compound
    element in the final lattice."""
    def __init__(
        self,
        name: str,
        core: List[ElementBuilder],
        entry_transform: List[ElementBuilder],
        exit_transform: List[ElementBuilder],
        aperture: List[ElementBuilder],
    ):
        self.name = name
        self.core = core
        self.entry_transform = entry_transform
        self.exit_transform = exit_transform
        self.aperture = aperture

    def add_to_line(self, line, buffer):
        start_marker = ElementBuilder(
            name=self.name + "_entry",
            type=xtrack.Marker,
        )

        end_marker = ElementBuilder(
            name=self.name + "_exit",
            type=xtrack.Marker,
        )

        component_elements = (
            [start_marker] +
            self.aperture +
            self.entry_transform + self.core + self.exit_transform +
            [end_marker]
        )

        for el in component_elements:
            el.add_to_line(line, buffer)

        def _get_names(builder_elements):
            return [elem.name for elem in builder_elements]

        compound = Compound(
            core=_get_names(self.core),
            aperture=_get_names(self.aperture),
            entry_transform=_get_names(self.entry_transform),
            exit_transform=_get_names(self.exit_transform),
            entry=start_marker.name,
            exit_=end_marker.name,
        )
        line.compound_container.define_compound(self.name, compound)


class Aperture:
    def __init__(self, mad_el, enable_errors, loader):
        self.mad_el = mad_el
        self.aper_tilt = rad2deg(mad_el.aper_tilt)
        self.aper_offset = mad_el.aper_offset
        self.name = self.mad_el.name
        self.dx = self.aper_offset[0]
        if len(self.aper_offset) > 1:
            self.dy = self.aper_offset[1]
        else:
            self.dy = 0
        if enable_errors and self.mad_el.align_errors is not None:
            self.dx += mad_el.align_errors.arex
            self.dy += mad_el.align_errors.arey
        self.apertype = self.mad_el.apertype
        self.loader = loader
        self.classes = loader.classes
        self.Builder = loader.Builder

    def entry(self):
        out = []
        if self.aper_tilt:
            out.append(
                self.Builder(
                    self.name + "_aper_tilt_entry",
                    self.classes.SRotation,
                    angle=self.aper_tilt,
                )
            )
        if self.dx or self.dy:
            out.append(
                self.Builder(
                    self.name + "_aper_offset_entry",
                    self.classes.XYShift,
                    dx=self.dx,
                    dy=self.dy,
                )
            )
        return out

    def exit(self):
        out = []
        if self.dx or self.dy:
            out.append(
                self.Builder(
                    self.name + "_aper_offset_exit",
                    self.classes.XYShift,
                    dx=-self.dx,
                    dy=-self.dy,
                )
            )
        if self.aper_tilt:
            out.append(
                self.Builder(
                    self.name + "_aper_tilt_exit",
                    self.classes.SRotation,
                    angle=-self.aper_tilt,
                )
            )
        return out

    def aperture(self):
        if len(self.mad_el.aper_vx) > 2:
            return [
                self.Builder(
                    self.name + "_aper",
                    self.classes.LimitPolygon,
                    x_vertices=self.mad_el.aper_vx,
                    y_vertices=self.mad_el.aper_vy,
                )
            ]
        else:
            conveter = getattr(self.loader, "convert_" + self.apertype, None)
            if conveter is None:
                raise ValueError(f"Aperture type `{self.apertype}` not supported")
            return conveter(self.mad_el)


class Alignment:
    def __init__(self, mad_el, enable_errors, classes, Builder, custom_tilt=None):
        self.mad_el = mad_el
        self.tilt = mad_el.get("tilt", 0)  # some elements do not have tilt
        if self.tilt:
            self.tilt = rad2deg(self.tilt)
        if custom_tilt is not None:
            self.tilt += rad2deg(custom_tilt)
        self.name = mad_el.name
        self.dx = 0
        self.dy = 0
        if (
            enable_errors
            and hasattr(mad_el, "align_errors")
            and mad_el.align_errors is not None
        ):
            self.align_errors = mad_el.align_errors
            self.dx = self.align_errors.dx
            self.dy = self.align_errors.dy
            self.tilt += rad2deg(self.align_errors.dpsi)
        self.classes = classes
        self.Builder = Builder

    def entry(self):
        out = []
        if self.tilt:
            out.append(
                self.Builder(
                    self.name + "_tilt_entry",
                    self.classes.SRotation,
                    angle=self.tilt,
                )
            )
        if self.dx or self.dy:
            out.append(
                self.Builder(
                    self.name + "_offset_entry",
                    self.classes.XYShift,
                    dx=self.dx,
                    dy=self.dy,
                )
            )
        return out

    def exit(self):
        out = []
        if self.dx or self.dy:
            out.append(
                self.Builder(
                    self.name + "_offset_exit",
                    self.classes.XYShift,
                    dx=-self.dx,
                    dy=-self.dy,
                )
            )
        if self.tilt:
            out.append(
                self.Builder(
                    self.name + "_tilt_exit",
                    self.classes.SRotation,
                    angle=-self.tilt,
                )
            )
        return out


class Dummy:
    type = "None"

def _default_factory():
    return 0.

class MadLoader:
    @staticmethod
    def init_line_expressions(line, mad, replace_in_expr):  # to be added to Line....
        """Enable expressions"""
        if line._var_management is None:
            line._init_var_management()

        from xdeps.madxutils import MadxEval

        _var_values = line._var_management["data"]["var_values"]
        _var_values.default_factory = _default_factory
        for name, par in mad.globals.cmdpar.items():
            if replace_in_expr is not None:
                for k, v in replace_in_expr.items():
                    name = name.replace(k, v)
            _var_values[name] = par.value
        _ref_manager = line._var_management["manager"]
        _vref = line._var_management["vref"]
        _fref = line._var_management["fref"]
        _lref = line._var_management["lref"]

        madeval_no_repl = MadxEval(_vref, _fref, None).eval

        if replace_in_expr is not None:
            def madeval(expr):
                for k, v in replace_in_expr.items():
                    expr = expr.replace(k, v)
                return madeval_no_repl(expr)
        else:
            madeval = madeval_no_repl

        # Extract expressions from madx globals
        for name, par in mad.globals.cmdpar.items():
            ee = par.expr
            if ee is not None:
                if "table(" in ee:  # Cannot import expressions involving tables
                    continue
                _vref[name] = madeval(ee)
        return madeval

    def __init__(
        self,
        sequence,
        enable_expressions=False,
        enable_errors=None,
        enable_field_errors=None,
        enable_align_errors=None,
        enable_apertures=False,
        skip_markers=False,
        merge_drifts=False,
        merge_multipoles=False,
        error_table=None,
        ignore_madtypes=(),
        expressions_for_element_types=None,
        classes=xtrack,
        replace_in_expr=None,
        allow_thick=False,
        use_compound_elements=True,
        name_prefix=None
    ):

        if enable_errors is not None:
            if enable_field_errors is None:
                enable_field_errors = enable_errors
            if enable_align_errors is None:
                enable_align_errors = enable_errors

        if enable_field_errors is None:
            enable_field_errors = False
        if enable_align_errors is None:
            enable_align_errors = False

        if allow_thick and enable_field_errors:
            raise NotImplementedError(
                "Field errors are not yet supported for thick elements"
            )

        if expressions_for_element_types is not None:
            assert enable_expressions, ("Expressions must be enabled if "
                                "`expressions_for_element_types` is not None")

        self.sequence = sequence
        self.enable_expressions = enable_expressions
        self.enable_field_errors = enable_field_errors
        self.enable_align_errors = enable_align_errors
        self.error_table = error_table
        self.skip_markers = skip_markers
        self.merge_drifts = merge_drifts
        self.merge_multipoles = merge_multipoles
        self.enable_apertures = enable_apertures
        self.expressions_for_element_types = expressions_for_element_types
        self.classes = classes
        self.replace_in_expr = replace_in_expr
        self._drift = self.classes.Drift
        self.ignore_madtypes = ignore_madtypes
        self.name_prefix = name_prefix

        self.allow_thick = allow_thick
        self.use_compound_elements = use_compound_elements

    def iter_elements(self, madeval=None):
        """Yield element data for each known element"""
        if len(self.sequence.expanded_elements)==0:
            raise ValueError(f"{self.sequence} has no elements, please do {self.sequence}.use()")
        last_element = Dummy
        for el in self.sequence.expanded_elements:
            madelem = MadElem(el.name, el, self.sequence, madeval,
                              name_prefix=self.name_prefix)
            if self.skip_markers and madelem.is_empty_marker():
                pass
            elif (
                self.merge_drifts
                and last_element.type == "drift"
                and madelem.type == "drift"
            ):
                last_element.l += el.l
            elif (
                self.merge_multipoles
                and last_element.type == "multipole"
                and madelem.type == "multipole"
            ):
                merged = last_element.merge_multipole(madelem)
                if not merged:
                    yield last_element
                    last_element = madelem
            elif madelem.type in self.ignore_madtypes:
                pass
            else:
                if last_element is not Dummy:
                    yield last_element
                last_element = madelem
        yield last_element

    def make_line(self, buffer=None):
        """Create a new line in buffer"""

        mad = self.sequence._madx

        if buffer is None:
            buffer = xobjects.context_default.new_buffer()

        line = self.classes.Line()
        self.line = line

        if self.enable_expressions:
            madeval = MadLoader.init_line_expressions(line, mad,
                                                      self.replace_in_expr)
            self.Builder = ElementBuilderWithExpr
        else:
            madeval = None
            self.Builder = ElementBuilder

        nelem = len(self.sequence.expanded_elements)

        for ii, el in enumerate(self.iter_elements(madeval=madeval)):

            # for each mad element create xtract elements in a buffer and add to a line
            converter = getattr(self, "convert_" + el.type, None)
            adder = getattr(self, "add_" + el.type, None)
            if self.expressions_for_element_types is not None:
               if el.type in self.expressions_for_element_types:
                   self.Builder = ElementBuilderWithExpr
                   el.madeval = madeval
               else:
                    self.Builder = ElementBuilder
                    el.madeval = None
            if adder:
                adder(el, line, buffer)
            elif converter:
                converted_el = converter(el)
                self.add_elements(converted_el, line, buffer)
            else:
                raise ValueError(
                    f'Element {el.type} not supported,\nimplement "add_{el.type}"'
                    f" or convert_{el.type} in function in MadLoader"
                )
            if ii % 100 == 0:
                _print(
                    f'Converting sequence "{self.sequence.name}":'
                    f' {round(ii/nelem*100):2d}%     ',
                    end="\r",
                    flush=True,
                )
        _print()
        return line

    def add_elements(
        self,
        elements: List[Union[ElementBuilder, CompoundElementBuilder]],
        line,
        buffer,
    ):
        out = {}  # tbc
        for el in elements:
            xt_element = el.add_to_line(line, buffer)
            out[el.name] = xt_element  # tbc
        return out  # tbc

    @property
    def math(self):
        if issubclass(self.Builder, ElementBuilderWithExpr):
            return self.line._var_management['fref']

        import math
        return math

    def _assert_element_is_thin(self, mad_el):
        if value_if_expr(mad_el.l) != 0:
            if self.allow_thick:
                raise NotImplementedError(
                    f'Cannot load element {mad_el.name}, as thick elements of '
                    f'type {"/".join(mad_el.get_type_hierarchy())} are not '
                    f'yet supported.'
                )
            else:
                raise ValueError(
                    f'Element {mad_el.name} is thick, but importing thick '
                    f'elements is disabled. Did you forget to set '
                    f'`allow_thick=True`?'
                )

    def _make_drift_slice(self, mad_el, weight, name_pattern):
        return self.Builder(
            name_pattern.format(mad_el.name),
            self.classes.Drift,
            length=mad_el.l * weight,
        )

    def make_compound_elem(
            self,
            xtrack_el,
            mad_el,
            custom_tilt=None,
    ):
        """Add aperture and transformations to a thin element:
        tilt, offset, aperture, offset, tilt, tilt, offset, kick, offset, tilt

        Parameters
        ----------
        xtrack_el: list
            List of xtrack elements to which the aperture and transformations
            should be added.
        mad_el: MadElement
            The element for which the aperture and transformations should be
            added.
        custom_tilt: float, optional
            If not None, the element will be additionally tilted by this
            amount.
        """
        # TODO: Implement permanent alignment

        align = Alignment(
            mad_el, self.enable_align_errors, self.classes, self.Builder, custom_tilt)

        aperture_seq = []
        if self.enable_apertures and mad_el.has_aperture():
            aper = Aperture(mad_el, self.enable_align_errors, self)
            aperture_seq = aper.entry() + aper.aperture() + aper.exit()

        align_entry, align_exit = align.entry(), align.exit()
        elem_list = aperture_seq + align_entry + xtrack_el + align_exit

        if not self.use_compound_elements:
            return elem_list

        is_singleton = len(elem_list) == 1
        if is_singleton:

            is_drift = issubclass(elem_list[0].type, self.classes.Drift)
            if is_drift and mad_el.name.startswith('drift_'):
                return elem_list

            is_marker = issubclass(elem_list[0].type, self.classes.Marker)
            if is_marker:
                return elem_list

        return [
            CompoundElementBuilder(
                name=mad_el.name,
                core=xtrack_el,
                entry_transform=align.entry(),
                exit_transform=align.exit(),
                aperture=aperture_seq,
            ),
        ]

    def convert_quadrupole(self, mad_el):
        if self.allow_thick:
            if not mad_el.l:
                raise ValueError(
                    "Thick quadrupole with legth zero are not supported.")
            return self._convert_quadrupole_thick(mad_el)
        else:
            raise NotImplementedError(
                "Quadrupole are not supported in thin mode."
            )

    def _convert_quadrupole_thick(self, mad_el):
        if mad_el.k1s:
            tilt = -self.math.atan2(mad_el.k1s, mad_el.k1) / 2
            k1 = 0.5 * self.math.sqrt(mad_el.k1s ** 2 + mad_el.k1 ** 2)
        else:
            tilt = None
            k1 = mad_el.k1

        return self.make_compound_elem(
            [
                self.Builder(
                    mad_el.name,
                    self.classes.Quadrupole,
                    k1=k1,
                    length=mad_el.l,
                ),
            ],
            mad_el,
            custom_tilt=tilt,
        )

    def convert_rbend(self, mad_el):
        return self._convert_bend(mad_el)

    def convert_sbend(self, mad_el):
        return self._convert_bend(mad_el)

    def _convert_bend(
        self,
        mad_el,
    ):

        assert self.allow_thick, "Bends are not supported in thin mode."

        l_curv = mad_el.l
        h = mad_el.angle / l_curv

        if mad_el.type == 'rbend' and self.sequence._madx.options.rbarc and value_if_expr(mad_el.angle):
            R = 0.5 * mad_el.l / self.math.sin(0.5 * mad_el.angle) # l is on the straight line
            l_curv = R * mad_el.angle
            h = 1 / R

        if not mad_el.k0:
            k0 = h
        else:
            k0 = mad_el.k0

        # Convert bend core
        num_multipole_kicks = 0
        if mad_el.k2:
            num_multipole_kicks = DEFAULT_BEND_N_MULT_KICKS
        if mad_el.k1:
            cls = self.classes.CombinedFunctionMagnet
            kwargs = dict(k1=mad_el.k1)
        else:
            cls = self.classes.Bend
            kwargs = {}
        bend_core = self.Builder(
            mad_el.name,
            cls,
            k0=k0,
            h=h,
            length=l_curv,
            knl=[0, 0, mad_el.k2 * l_curv],
            num_multipole_kicks=num_multipole_kicks,
            **kwargs,
        )

        sequence = [bend_core]

        # Convert dipedge
        if mad_el.type == 'sbend':
            e1 = mad_el.e1
            e2 = mad_el.e2
        elif mad_el.type == 'rbend':
            e1 = mad_el.e1 + mad_el.angle / 2
            e2 = mad_el.e2 + mad_el.angle / 2
        else:
            raise NotImplementedError(
                f'Unknown bend type {mad_el.type}.'
            )

        dipedge_entry = self.Builder(
            mad_el.name + "_den",
            self.classes.DipoleEdge,
            e1=e1,
            e1_fd = (k0 - h) * l_curv / 2,
            fint=mad_el.fint,
            hgap=mad_el.hgap,
            k=k0,
            side='entry'
        )
        sequence = [dipedge_entry] + sequence

        # For the sbend edge import we assume k0l = angle
        dipedge_exit = self.Builder(
            mad_el.name + "_dex",
            self.classes.DipoleEdge,
            e1=e2,
            e1_fd = (k0 - h) * l_curv / 2,
            fint=mad_el.fintx if value_if_expr(mad_el.fintx) >= 0 else mad_el.fint,
            hgap=mad_el.hgap,
            k=k0,
            side='exit'
        )
        sequence = sequence + [dipedge_exit]

        return self.make_compound_elem(sequence, mad_el)

    def convert_sextupole(self, mad_el):
        return self.make_compound_elem(
            [
                self.Builder(
                    mad_el.name,
                    self.classes.Sextupole,
                    k2=mad_el.k2,
                    k2s=mad_el.k2s,
                    length=mad_el.l,
                ),
            ],
            mad_el,
        )

    def convert_octupole(self, mad_el):
        thin_oct = self.Builder(
            mad_el.name,
            self.classes.Multipole,
            knl=[0, 0, 0, mad_el.k3 * mad_el.l],
            ksl=[0, 0, 0, mad_el.k3s * mad_el.l],
            length=mad_el.l,
        )

        if value_if_expr(mad_el.l) != 0:
            if not self.allow_thick:
                self._assert_element_is_thin(mad_el)

            sequence = [
                self._make_drift_slice(mad_el, 0.5, "drift_{}..1"),
                thin_oct,
                self._make_drift_slice(mad_el, 0.5, "drift_{}..2"),
            ]
        else:
            sequence = [thin_oct]

        return self.make_compound_elem(sequence, mad_el)

    def convert_rectangle(self, mad_el):
        h, v = mad_el.aperture[:2]
        return [
            self.Builder(
                mad_el.name + "_aper",
                self.classes.LimitRect,
                min_x=-h,
                max_x=h,
                min_y=-v,
                max_y=v,
            )
        ]

    def convert_racetrack(self, mad_el):
        h, v, a, b = mad_el.aperture[:4]
        return [
            self.Builder(
                mad_el.name + "_aper",
                self.classes.LimitRacetrack,
                min_x=-h,
                max_x=h,
                min_y=-v,
                max_y=v,
                a=a,
                b=b,
            )
        ]

    def convert_ellipse(self, mad_el):
        a, b = mad_el.aperture[:2]
        return [
            self.Builder(mad_el.name + "_aper", self.classes.LimitEllipse, a=a, b=b)
        ]

    def convert_circle(self, mad_el):
        a = mad_el.aperture[0]
        return [
            self.Builder(mad_el.name + "_aper", self.classes.LimitEllipse, a=a, b=a)
        ]

    def convert_rectellipse(self, mad_el):
        h, v, a, b = mad_el.aperture[:4]
        return [
            self.Builder(
                mad_el.name + "_aper",
                self.classes.LimitRectEllipse,
                max_x=h,
                max_y=v,
                a=a,
                b=b,
            )
        ]

    def convert_octagon(self, ee):
        a0 = ee.aperture[0]
        a1 = ee.aperture[1]
        a2 = ee.aperture[2]
        a3 = ee.aperture[3]
        V1 = (a0, a0 * np.tan(a2))  # expression will fail
        V2 = (a1 / np.tan(a3), a1)  # expression will fail
        el = self.Builder(
            ee.name + "_aper",
            self.classes.LimitPolygon,
            x_vertices=[V1[0], V2[0], -V2[0], -V1[0], -V1[0], -V2[0], V2[0], V1[0]],
            y_vertices=[V1[1], V2[1], V2[1], V1[1], -V1[1], -V2[1], -V2[1], -V1[1]],
        )
        return [el]

    def convert_polygon(self, ee):
        x_vertices = ee.aper_vx[0::2]
        y_vertices = ee.aper_vy[1::2]
        el = self.Builder(
            ee.name + "_aper",
            self.classes.LimitPolygon,
            x_vertices=x_vertices,
            y_vertices=y_vertices,
        )
        return [el]

    def convert_drift(self, mad_elem):
        return [self.Builder(mad_elem.name, self._drift, length=mad_elem.l)]

    def convert_marker(self, mad_elem):
        el = self.Builder(mad_elem.name, self.classes.Marker)
        return self.make_compound_elem([el], mad_elem)

    def convert_drift_like(self, mad_elem):
        el = self.Builder(mad_elem.name, self._drift, length=mad_elem.l)
        return self.make_compound_elem([el], mad_elem)

    convert_monitor = convert_drift_like
    convert_hmonitor = convert_drift_like
    convert_vmonitor = convert_drift_like
    convert_collimator = convert_drift_like
    convert_rcollimator = convert_drift_like
    convert_elseparator = convert_drift_like
    convert_instrument = convert_drift_like

    def convert_solenoid(self, mad_elem):
        if get_value(mad_elem.l) == 0:
            _print(f'Warning: Thin solenoids are not yet implemented, '
                   f'reverting to importing `{mad_elem.name}` as a drift.')
            return self.convert_drift_like(mad_elem)

        el = self.Builder(
            mad_elem.name,
            self.classes.Solenoid,
            length=mad_elem.l,
            ks=mad_elem.ks,
            ksi=mad_elem.ksi,
        )
        return self.make_compound_elem([el], mad_elem)

    def convert_multipole(self, mad_elem):
        self._assert_element_is_thin(mad_elem)
        # getting max length of knl and ksl
        knl = mad_elem.knl
        ksl = mad_elem.ksl
        lmax = max(non_zero_len(knl), non_zero_len(ksl), 1)
        if mad_elem.field_errors is not None and self.enable_field_errors:
            dkn = mad_elem.field_errors.dkn
            dks = mad_elem.field_errors.dks
            lmax = max(lmax, non_zero_len(dkn), non_zero_len(dks))
            knl = add_lists(knl, dkn, lmax)
            ksl = add_lists(ksl, dks, lmax)
        el = self.Builder(mad_elem.name, self.classes.Multipole, order=lmax - 1)
        el.knl = knl[:lmax]
        el.ksl = ksl[:lmax]
        if (
            mad_elem.angle
        ):  # testing for non-zero (cannot use !=0 as it creates an expression)
            el.hxl = mad_elem.angle
        else:
            el.hxl = mad_elem.knl[0]  # in madx angle=0 -> dipole
            el.hyl = mad_elem.ksl[0]  # in madx angle=0 -> dipole
        el.length = mad_elem.lrad
        return self.make_compound_elem([el], mad_elem)

    def convert_kicker(self, mad_el):
        hkick = [-mad_el.hkick] if mad_el.hkick else []
        vkick = [mad_el.vkick] if mad_el.vkick else []
        thin_kicker = self.Builder(
            mad_el.name,
            self.classes.Multipole,
            knl=hkick,
            ksl=vkick,
            length=mad_el.lrad,
            hxl=0,
            hyl=0,
        )

        if value_if_expr(mad_el.l) != 0:
            if not self.allow_thick:
                self._assert_element_is_thin(mad_el)

            sequence = [
                self._make_drift_slice(mad_el, 0.5, "drift_{}..1"),
                thin_kicker,
                self._make_drift_slice(mad_el, 0.5, "drift_{}..2"),
            ]
        else:
            sequence = [thin_kicker]

        return self.make_compound_elem(sequence, mad_el)

    convert_tkicker = convert_kicker

    def convert_hkicker(self, mad_el):
        if mad_el.hkick:
            raise ValueError(
                "hkicker with hkick is not supported, please use kick instead")

        hkick = [-mad_el.kick] if mad_el.kick else []
        vkick = []
        thin_hkicker = self.Builder(
            mad_el.name,
            self.classes.Multipole,
            knl=hkick,
            ksl=vkick,
            length=mad_el.lrad,
            hxl=0,
            hyl=0,
        )

        if value_if_expr(mad_el.l) != 0:
            if not self.allow_thick:
                self._assert_element_is_thin(mad_el)

            sequence = [
                self._make_drift_slice(mad_el, 0.5, "drift_{}..1"),
                thin_hkicker,
                self._make_drift_slice(mad_el, 0.5, "drift_{}..2"),
            ]
        else:
            sequence = [thin_hkicker]

        return self.make_compound_elem(sequence, mad_el)

    def convert_vkicker(self, mad_el):
        if mad_el.vkick:
            raise ValueError(
                "vkicker with vkick is not supported, please use kick instead")

        hkick = []
        vkick = [mad_el.kick] if mad_el.kick else []
        thin_vkicker = self.Builder(
            mad_el.name,
            self.classes.Multipole,
            knl=hkick,
            ksl=vkick,
            length=mad_el.lrad,
            hxl=0,
            hyl=0,
        )

        if value_if_expr(mad_el.l) != 0:
            if not self.allow_thick:
                self._assert_element_is_thin(mad_el)

            sequence = [
                self._make_drift_slice(mad_el, 0.5, "drift_{}..1"),
                thin_vkicker,
                self._make_drift_slice(mad_el, 0.5, "drift_{}..2"),
            ]
        else:
            sequence = [thin_vkicker]

        return self.make_compound_elem(sequence, mad_el)

    def convert_dipedge(self, mad_elem):
        # TODO LRAD
        el = self.Builder(
            mad_elem.name,
            self.classes.DipoleEdge,
            h=mad_elem.h,
            e1=mad_elem.e1,
            hgap=mad_elem.hgap,
            fint=mad_elem.fint,
        )
        return self.make_compound_elem([el], mad_elem)

    def convert_rfcavity(self, ee):
        # TODO LRAD
        if ee.freq == 0 and ee.harmon:
            frequency = (
                ee.harmon * self.sequence.beam.beta * clight / self.sequence.length
            )
        else:
            frequency = ee.freq * 1e6
        if (hasattr(self.sequence, 'beam')
                and self.sequence.beam.particle == 'ion'):
            scale_voltage = 1./self.sequence.beam.charge
        else:
            scale_voltage = 1.
        el = self.Builder(
            ee.name,
            self.classes.Cavity,
            voltage=scale_voltage * ee.volt * 1e6,
            frequency=frequency,
            lag=ee.lag * 360,
        )

        if value_if_expr(ee.l) != 0:
            sequence = [
                self._make_drift_slice(ee, 0.5, f"drift_{{}}..1"),
                el,
                self._make_drift_slice(ee, 0.5, f"drift_{{}}..2"),
            ]
        else:
            sequence = [el]

        return self.make_compound_elem(sequence, ee)

    def convert_rfmultipole(self, ee):
        self._assert_element_is_thin(ee)
        # TODO LRAD
        if ee.harmon:
            raise NotImplementedError
        if ee.l:
            raise NotImplementedError
        el = self.Builder(
            ee.name,
            self.classes.RFMultipole,
            voltage=ee.volt * 1e6,
            frequency=ee.freq * 1e6,
            lag=ee.lag * 360,
            knl=ee.knl,
            ksl=ee.ksl,
            pn=[v * 360 for v in ee.pnl],
            ps=[v * 360 for v in ee.psl],
        )
        return self.make_compound_elem([el], ee)

    def convert_wire(self, ee):
        self._assert_element_is_thin(ee)
        if len(ee.L_phy) == 1:
            # the index [0] is present because in MAD-X multiple wires can
            # be defined within the same element
            el = self.Builder(
                ee.name,
                self.classes.Wire,
                L_phy=ee.L_phy[0],
                L_int=ee.L_int[0],
                current=ee.current[0],
                xma=ee.xma[0],
                yma=ee.yma[0],
            )
            return self.make_compound_elem([el], ee)
        else:
            # TODO: add multiple elements for multiwire configuration
            raise ValueError("Multiwire configuration not supported")

    def convert_crabcavity(self, ee):
        self._assert_element_is_thin(ee)
        # This has to be disabled, as it raises an error when l is assigned to an
        # expression:
        # for nn in ["l", "harmon", "lagf", "rv1", "rv2", "rph1", "rph2"]:
        #     if getattr(ee, nn):
        #         raise NotImplementedError(f"Invalid value {nn}={getattr(ee, nn)}")

        # ee.volt in MV, sequence.beam.pc in GeV
        if abs(ee.tilt - np.pi / 2) < 1e-9:
            el = self.Builder(
                ee.name,
                self.classes.RFMultipole,
                frequency=ee.freq * 1e6,
                ksl=[-ee.volt / self.sequence.beam.pc * 1e-3],
                ps=[ee.lag * 360 + 90],
            )
            ee.tilt = 0
        else:
            el = self.Builder(
                ee.name,
                self.classes.RFMultipole,
                frequency=ee.freq * 1e6,
                knl=[ee.volt / self.sequence.beam.pc * 1e-3],
                pn=[ee.lag * 360 + 90],  # TODO: Changed sign to match sixtrack
                # To be checked!!!!
            )
        return self.make_compound_elem([el], ee)

    def convert_beambeam(self, ee):
        self._assert_element_is_thin(ee)
        import xfields as xf

        if ee.slot_id == 6 or ee.slot_id == 60:
            # force no expression by using ElementBuilder and not self.Builder
            el = ElementBuilder(
                ee.name,
                xf.BeamBeamBiGaussian3D,
                old_interface={
                    "phi": 0.0,
                    "alpha": 0.0,
                    "x_bb_co": 0.0,
                    "y_bb_co": 0.0,
                    "charge_slices": [0.0],
                    "zeta_slices": [0.0],
                    "sigma_11": 1.0,
                    "sigma_12": 0.0,
                    "sigma_13": 0.0,
                    "sigma_14": 0.0,
                    "sigma_22": 1.0,
                    "sigma_23": 0.0,
                    "sigma_24": 0.0,
                    "sigma_33": 0.0,
                    "sigma_34": 0.0,
                    "sigma_44": 0.0,
                    "x_co": 0.0,
                    "px_co": 0.0,
                    "y_co": 0.0,
                    "py_co": 0.0,
                    "zeta_co": 0.0,
                    "delta_co": 0.0,
                    "d_x": 0.0,
                    "d_px": 0.0,
                    "d_y": 0.0,
                    "d_py": 0.0,
                    "d_zeta": 0.0,
                    "d_delta": 0.0,
                },
            )
        else:
            # BB interaction is 4D
            # force no expression by using ElementBuilder and not self.Builder
            el = ElementBuilder(
                ee.name,
                xf.BeamBeamBiGaussian2D,
                n_particles=0.0,
                q0=0.0,
                beta0=1.0,
                mean_x=0.0,
                mean_y=0.0,
                sigma_x=1.0,
                sigma_y=1.0,
                d_px=0,
                d_py=0,
            )
        return self.make_compound_elem([el], ee)

    def convert_placeholder(self, ee):
        # assert not is_expr(ee.slot_id) can be done only after release MADX 5.09
        if ee.slot_id == 1:
            raise ValueError("This feature is discontinued!")
            # newele = classes.SCCoasting()
        elif ee.slot_id == 2:
            # TODO Abstraction through `classes` to be introduced
            raise ValueError("This feature is discontinued!")
            # import xfields as xf
            # lprofile = xf.LongitudinalProfileQGaussian(
            #         number_of_particles=0.,
            #         sigma_z=1.,
            #         z0=0.,
            #         q_parameter=1.)
            # newele = xf.SpaceChargeBiGaussian(
            #     length=0,
            #     apply_z_kick=False,
            #     longitudinal_profile=lprofile,
            #     mean_x=0.,
            #     mean_y=0.,
            #     sigma_x=1.,
            #     sigma_y=1.)

        elif ee.slot_id == 3:
            el = self.Builder(ee.name, self.classes.SCInterpolatedProfile)
        else:
            el = self.Builder(ee.name, self._drift, length=ee.l)
        return self.make_compound_elem([el], ee)

    def convert_matrix(self, ee):
        length = ee.l
        m0 = np.zeros(6, dtype=object)
        for m0_i in range(6):
            att_name = f"kick{m0_i+1}"
            if hasattr(ee, att_name):
                m0[m0_i] = getattr(ee, att_name)
        m1 = np.zeros((6, 6), dtype=object)
        for m1_i in range(6):
            for m1_j in range(6):
                att_name = f"rm{m1_i+1}{m1_j+1}"
                if hasattr(ee, att_name):
                    m1[m1_i, m1_j] = getattr(ee, att_name)
        el = self.Builder(
            ee.name, self.classes.FirstOrderTaylorMap, length=length, m0=m0, m1=m1
        )
        return self.make_compound_elem([el], ee)

    def convert_srotation(self, ee):
        angle = ee.angle*180/np.pi
        el = self.Builder(
            ee.name, self.classes.SRotation, angle=angle
        )
        return self.make_compound_elem([el], ee)

    def convert_xrotation(self, ee):
        angle = ee.angle*180/np.pi
        el = self.Builder(
            ee.name, self.classes.XRotation, angle=angle
        )
        return self.make_compound_elem([el], ee)

    def convert_yrotation(self, ee):
        angle = ee.angle*180/np.pi
        el = self.Builder(
            ee.name, self.classes.YRotation, angle=angle
        )
        return self.make_compound_elem([el], ee)

    def convert_translation(self, ee):
        el_transverse = self.Builder(
            ee.name, self.classes.XYShift, dx=ee.dx, dy=ee.dy
        )
        if ee.ds:
            raise NotImplementedError # Need to implement ShiftS element
        ee.dx = 0
        ee.dy = 0
        ee.ds = 0
        return self.make_compound_elem([el_transverse], ee)

    def convert_nllens(self, mad_elem):
        el = self.Builder(
            mad_elem.name,
            self.classes.NonLinearLens,
            knll=mad_elem.knll,
            cnll=mad_elem.cnll,
        )
        return self.make_compound_elem([el], mad_elem)
