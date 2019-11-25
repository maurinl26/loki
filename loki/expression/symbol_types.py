import weakref
from collections import OrderedDict
import pymbolic.primitives as pmbl
from pymbolic.mapper.stringifier import (StringifyMapper, PREC_NONE, PREC_CALL)
from six.moves import intern

from loki.tools import as_tuple
from loki.types import DataType, SymbolType


__all__ = ['Scalar', 'Array', 'Variable', 'Literal', 'IntLiteral', 'FloatLiteral', 'LogicLiteral',
           'LiteralList', 'RangeIndex', 'InlineCall', 'LokiStringifyMapper', 'Cast']


class LokiStringifyMapper(StringifyMapper):
    """
    A class derived from the default :class:`StringifyMapper` that adds mappings for nodes of the
    expression tree that we added ourselves.
    """

    def __init__(self, constant_mapper=None):
        super(LokiStringifyMapper, self).__init__(constant_mapper)

    def map_logic_literal(self, expr, *args, **kwargs):
        return str(expr.value)

    def map_float_literal(self, expr, enclosing_prec, *args, **kwargs):
        if expr.kind is not None:
            return '%s_%s' % (str(expr.value), self.rec(expr.kind, PREC_CALL, *args, **kwargs))
        else:
            return str(expr.value)

    map_int_literal = map_logic_literal

    def map_string_literal(self, expr, *args, **kwargs):
        return "'%s'" % expr.value

    def map_scalar(self, expr, *args, **kwargs):
        if expr.parent is not None:
            parent = self.rec(expr.parent, *args, **kwargs)
            return self.format('%s%%%s', parent, expr.basename)
        else:
            return expr.name

    def map_array(self, expr, enclosing_prec, *args, **kwargs):
        dims = ','.join(self.rec(d, PREC_NONE, *args, **kwargs) for d in expr.dimensions or [])
        if dims:
            dims = '(' + dims + ')'
        parent, initial = '', ''
        if expr.parent is not None:
            parent = self.rec(expr.parent, PREC_NONE, *args, **kwargs) + '%'
        if expr.type is not None and expr.type.initial is not None:
            initial = ' = %s' % self.rec(expr.initial, PREC_NONE, *args, **kwargs)
        return self.format('%s%s%s%s', parent, expr.basename, dims, initial)

    map_inline_call = StringifyMapper.map_call_with_kwargs

    def map_cast(self, expr, enclosing_prec, *args, **kwargs):
        name = self.rec(expr.function, PREC_CALL, *args, **kwargs)
        expression = self.rec(expr.parameters[0], PREC_NONE, *args, **kwargs)
        if expr.kind:
            if isinstance(expr.kind, pmbl.Expression):
                kind = ', kind=' + self.rec(expr.kind, PREC_NONE, *args, **kwargs)
            else:
                kind = ', kind=' + str(expr.kind)
        else:
            kind = ''
        return self.format('%s(%s%s)', name, expression, kind)

    def map_range_index(self, expr, *args, **kwargs):
        lower = self.rec(expr.lower, *args, **kwargs) if expr.lower else ''
        upper = self.rec(expr.upper, *args, **kwargs) if expr.upper else ''
        if expr.step:
            return '%s:%s:%s' % (lower, upper, self.rec(expr.step, *args, **kwargs))
        else:
            return '%s:%s' % (lower, upper)

    def map_parenthesised_add(self, *args, **kwargs):
        return self.parenthesize(self.map_sum(*args, **kwargs))

    def map_parenthesised_mul(self, *args, **kwargs):
        return self.parenthesize(self.map_product(*args, **kwargs))

    def map_parenthesised_pow(self, *args, **kwargs):
        return self.parenthesize(self.map_power(*args, **kwargs))

    def map_literal_list(self, expr, *args, **kwargs):
        return '[' + ','.join(str(c) for c in expr.elements) + ']'


class Scalar(pmbl.Variable):
    """
    Expression node for scalar variables (and other algebraic leaves).

    It is always associated with a given scope (typically a class:``Subroutine``)
    where the corresponding `symbol_table` is found with its type.

    Warning: Providing a type overwrites the corresponding entry in the symbol table.
    This is due to the assumption that we might have encountered a variable name before
    knowing about its declaration and thus treat the latest given type information as
    the one that is most up-to-date.

    Note that a Variable instance and a type can have a parent, with the parent of a Variable
    always being a Variable and the parent of a type always being a type. Conceptually, it should
    always hold `variable.parent.type == variable.type.parent.type`. This is due to the fact that
    a variables parent can be an Array of derived types and as such has a `dimensions` parameter,
    thus requires a different parent than at the time of declaration.
    """

    def __init__(self, name, scope, type=None, parent=None, initial=None, source=None):
        super(Scalar, self).__init__(name)

#        self._scope = weakref.ref(scope)
        self.scope = scope
        if type is None:
            # Insert the deferred type in the type table only if it does not exist
            # yet (necessary for deferred type definitions, e.g., derived types in header or
            # parameters from other modules)
            self.scope.symbols.setdefault(self.name, SymbolType(DataType.DEFERRED))
        else:
            self.type = type
        self.parent = parent
        self.initial = initial
        self.source = source

#    @property
#    def scope(self):
#        """
#        The object corresponding to the symbols scope.
#        """
#        return self._scope()

    @property
    def basename(self):
        """
        The symbol name without the qualifier from the parent.
        """
        idx = self.name.rfind('%')
        return self.name[idx+1:]

    @property
    def type(self):
        """
        Internal representation of the declared data type.
        """
        return self.scope.symbols[self.name]

    @type.setter
    def type(self, value):
        self.scope.symbols[self.name] = value

    def __getinitargs__(self):
        return tuple([self.name, ('scope', self.scope)])

    mapper_method = intern('map_scalar')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()

    def clone(self, **kwargs):
        """
        Replicate the :class:`Scalar` variable with the provided overrides.
        """
        # Add existing meta-info to the clone arguments, only if we have them.
        if self.name and 'name' not in kwargs:
            kwargs['name'] = self.name
        if self.scope and 'scope' not in kwargs:
            kwargs['scope'] = self.scope
        if self.type and 'type' not in kwargs:
            kwargs['type'] = self.type
        if self.parent and 'parent' not in kwargs:
            kwargs['parent'] = self.parent

        return Variable(**kwargs)


class Array(pmbl.Variable):
    """
    Expression node for array variables.

    It can have associated dimensions (i.e., the indexing/slicing when accessing entries),
    which can be a :class:`RangeIndex` or an expression or a :class:`Literal` or
    a :class:`Scalar`

    Shape, data type and parent information are part of the type.

    Warning: Providing a type overwrites the corresponding entry in the symbol table.
    This is due to the assumption that we might have encountered a variable name before
    knowing about its declaration and thus treat the latest given type information as
    the one that is most up-to-date.

    Note that a Variable instance and a type can have a parent, with the parent of a Variable
    always being a Variable and the parent of a type always being a type. Conceptually, it should
    always hold `variable.parent.type == variable.type.parent.type`. This is due to the fact that
    a variables parent can be an Array of derived types and as such has a `dimensions` parameter,
    thus requires a different parent than at the time of declaration.
    """

    def __init__(self, name, scope, type=None, parent=None, dimensions=None,
                 initial=None, source=None):
        super(Array, self).__init__(name)

        self._scope = weakref.ref(scope)
#        self.scope = scope
        if type is None:
            # Insert the defered type in the type table only if it does not exist
            # yet (necessary for deferred type definitions)
            self.scope.symbols.setdefault(self.name, SymbolType(DataType.DEFERRED))
        else:
            self.type = type
        self.parent = parent
        self.dimensions = dimensions
        self.initial = initial
        self.source = source

    @property
    def scope(self):
        """
        The object corresponding to the symbols scope.
        """
        return self._scope()

    @property
    def basename(self):
        """
        The symbol name without the qualifier from the parent.
        """
        idx = self.name.rfind('%')
        return self.name[idx+1:]

    @property
    def type(self):
        """
        Internal representation of the declared data type.
        """
        return self.scope.symbols[self.name]

    @type.setter
    def type(self, value):
        self.scope.symbols[self.name] = value

    @property
    def dimensions(self):
        """
        Symbolic representation of the dimensions or indices.
        """
        return self._dimensions

    @dimensions.setter
    def dimensions(self, value):
        self._dimensions = value

    @property
    def shape(self):
        """
        Original allocated shape of the variable as a tuple of dimensions.
        """
        return self.type.shape

    @shape.setter
    def shape(self, value):
        self.type.shape = value

    def __getinitargs__(self):
        args = [self.name, ('scope', self.scope)]
        if self.dimensions:
            args += [('dimensions', self.dimensions)]
        return tuple(args)

    mapper_method = intern('map_array')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()

    def clone(self, **kwargs):
        """
        Replicate the :class:`Array` variable with the provided overrides.

        Note, if :param dimensions: is provided as ``None``, a
        :class:`Scalar` variable will be created.
        """
        # Add existing meta-info to the clone arguments, only if we have them.
        if self.name and 'name' not in kwargs:
            kwargs['name'] = self.name
        if self.scope and 'scope' not in kwargs:
            kwargs['scope'] = self.scope
        if self.dimensions and 'dimensions' not in kwargs:
            kwargs['dimensions'] = self.dimensions
        if self.type and 'type' not in kwargs:
            kwargs['type'] = self.type
        if self.parent and 'parent' not in kwargs:
            kwargs['parent'] = self.parent

        return Variable(**kwargs)


class Variable(object):
    """
    A symbolic object representing either a :class:`Scalar` or a :class:`Array`
    variable in arithmetic expressions.

    Note, that this is only a convenience constructor that always returns either
    a :class:`Scalar` or :class:`Array` variable object.

    Warning: Providing a type overwrites the corresponding entry in the symbol table.
    This is due to the assumption that we might have encountered a variable name before
    knowing about its declaration and thus treat the latest given type information as
    the one that is most up-to-date.
    """

    def __new__(cls, *args, **kwargs):
        """
        1st-level variables creation with name injection via the object class
        """
        name = kwargs.pop('name')
        scope = kwargs.pop('scope')
        dimensions = kwargs.pop('dimensions', None)
        initial = kwargs.pop('initial', None)
        _type = kwargs.get('type', scope.symbols.lookup(name, recursive=False))
        parent = kwargs.pop('parent', None)
        source = kwargs.get('source', None)

        shape = _type.shape if _type is not None else None

        if dimensions is None and (shape is None or len(shape) == 0):
            obj = Scalar(name=name, type=_type, scope=scope, parent=parent,
                         initial=initial, source=source)
        else:
            obj = Array(name=name, dimensions=dimensions, type=_type, scope=scope, parent=parent,
                        initial=initial, source=source)

        obj = cls.instantiate_derived_type_variables(obj)
        return obj

    @classmethod
    def instantiate_derived_type_variables(cls, obj):
        """
        If the type of obj is a derived type then its list of variables is possibly from
        the declarations inside a TypeDef and as such, the variables are referring to a
        different scope. Thus, we must re-create these variables in the correct scope.
        For the actual instantiation of a variable with that type, we need to create a dedicated
        copy of that type and replace its parent by this object and its list of variables (which
        is an OrderedDict of SymbolTypes) by a list of Variable instances.
        """
        if obj.type is not None and obj.type.dtype == DataType.DERIVED_TYPE:
            if obj.type.variables and next(iter(obj.type.variables.values())).scope != obj.scope:
                variables = obj.type.variables
                obj.type = obj.type.clone(variables=OrderedDict())
                for k, v in variables.items():
                    vtype = v.type.clone(parent=obj)
                    vname = '%s%%%s' % (obj.name, v.basename)
                    obj.type.variables[k] = Variable(name=vname, scope=obj.scope, type=vtype)
        return obj


class FloatLiteral(pmbl.Leaf):
    """
    A floating point constant in an expression.

    It can have a specific type associated, which can be used to cast the constant to that
    type in the output of the backend.
    """

    def __init__(self, value, **kwargs):
        super(FloatLiteral, self).__init__()

        self.value = value
        self.kind = kwargs.get('kind', None)

    def __getinitargs__(self):
        args = [self.value]
        if self.kind:
            args += [('kind', self.kind)]
        return tuple(args)

    mapper_method = intern('map_float_literal')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()


class IntLiteral(pmbl.Leaf):
    """
    An integer constant in an expression.

    It can have a specific type associated, which can be used to cast the constant to that
    type in the output of the backend.
    """

    def __init__(self, value, **kwargs):
        super(IntLiteral, self).__init__()

        self.value = value
        self.kind = kwargs.get('kind', None)

    def __getinitargs__(self):
        args = [self.value]
        if self.kind:
            args += [('kind', self.kind)]
        return tuple(args)

    mapper_method = intern('map_int_literal')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()


class LogicLiteral(pmbl.Leaf):
    """
    A boolean constant in an expression.
    """

    def __init__(self, value, **kwargs):
        super(LogicLiteral, self).__init__()

        self.value = value.lower() in ('true', '.true.')

    def __getinitargs__(self):
        return (self.value,)

    mapper_method = intern('map_logic_literal')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()


class StringLiteral(pmbl.Leaf):
    """
    A string.
    """

    def __init__(self, value, **kwargs):
        super(StringLiteral, self).__init__()

        if value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]

        self.value = value

    def __getinitargs__(self):
        return (self.value,)

    mapper_method = intern('map_string_literal')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()


class Literal(object):
    """
    A factory class that instantiates the appropriate :class:`*Literal` type for
    a given value.

    This always returns a :class:`IntLiteral`, :class:`FloatLiteral`, :class:`StringLiteral`,
    or :class:`LogicLiteral`.
    """

    @staticmethod
    def _from_literal(value, **kwargs):

        cls_map = {DataType.INTEGER: IntLiteral, DataType.REAL: FloatLiteral,
                   DataType.LOGICAL: LogicLiteral, DataType.CHARACTER: StringLiteral}

        _type = kwargs.get('type', None)
        if _type is None:
            if isinstance(value, int):
                _type = DataType.INTEGER
            elif isinstance(value, float):
                _type = DataType.REAL
            elif isinstance(value, str):
                if str(value).lower() in ('.true.', 'true', '.false.', 'false'):
                    _type = DataType.LOGICAL
                else:
                    _type = DataType.CHARACTER

        return cls_map[_type](value, **kwargs)

    def __new__(cls, value, **kwargs):
        try:
            obj = cls._from_literal(value, **kwargs)
        except KeyError:
            # Let Pymbolic figure our what we're dealing with
            from pymbolic import parse
            obj = parse(value)

            # Make sure we catch elementary literals
            if not isinstance(obj, pmbl.Expression):
                obj = cls._from_literal(obj, **kwargs)

        # And attach our own meta-data
        if hasattr(obj, 'kind'):
            obj.kind = kwargs.get('kind', None)
        return obj


class LiteralList(pmbl.AlgebraicLeaf):
    """
    A list of constant literals, e.g., as used in Array Initialization Lists.
    """

    def __init__(self, values):
        super(LiteralList, self).__init__()

        self.elements = values

    mapper_method = intern('map_literal_list')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()

    def __getinitargs__(self):
        return ('[%s]' % (','.join(repr(c) for c in self.elements)),)


class InlineCall(pmbl.CallWithKwargs):
    """
    Internal representation of an in-line function call.
    """

    def __init__(self, function, parameters=None, kw_parameters=None):
        function = pmbl.make_variable(function)
        parameters = parameters or tuple()
        kw_parameters = kw_parameters or {}

        super(InlineCall, self).__init__(function, parameters, kw_parameters)

    mapper_method = intern('map_inline_call')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()

    @property
    def name(self):
        return self.function.name


class Cast(pmbl.Call):
    """
    Internal representation of a data type cast.
    """

    def __init__(self, name, expression, kind=None):
        super(Cast, self).__init__(pmbl.make_variable(name), as_tuple(expression))
        self.kind = kind

    mapper_method = intern('map_cast')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()

    @property
    def name(self):
        return self.function.name


class RangeIndex(pmbl.AlgebraicLeaf):
    """
    Internal representation of a subscript range.
    """

    @classmethod
    def _args2bounds(cls, *args, **kwargs):
        lower, upper, step = None, None, None
        if len(args) == 1:
            upper = args[0]
        elif len(args) == 2:
            lower = args[0]
            upper = args[1]
        elif len(args) == 3:
            lower = args[0]
            upper = args[1]
            step = args[2]

        lower = kwargs.get('lower', lower)
        upper = kwargs.get('upper', upper)
        step = kwargs.get('step', step)

        return lower, upper, step

    def __new__(cls, *args, **kwargs):
        lower, upper, step = RangeIndex._args2bounds(*args, **kwargs)

        # Short-circuit for direct indices
        if upper is not None and lower is None and step is None:
            return upper if isinstance(upper, pmbl.Expression) else Literal(upper)

        obj = object.__new__(cls)
        obj._lower = lower
        obj._upper = upper
        obj._step = step

        return obj

    def __init__(self, *args, **kwargs):
        super(RangeIndex, self).__init__()

        lower, upper, step = RangeIndex._args2bounds(*args, **kwargs)
        self._lower = lower
        self._upper = upper
        self._step = step

    def __getinitargs__(self):
        if self._step:
            return (self._lower, self._upper, self._step)
        elif self._lower:
            return (self._lower, self._upper)
        else:
            return (self._upper,)

    mapper_method = intern('map_range_index')

    def make_stringifier(self, originating_stringifier=None):
        return LokiStringifyMapper()

    @property
    def lower(self):
        return self._lower

    @property
    def upper(self):
        return self._upper

    @property
    def step(self):
        return self._step
