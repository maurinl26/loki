from fparser.two.parser import ParserFactory
from fparser.two.utils import get_child, walk_ast
from fparser.two import Fortran2003
from fparser.two.Fortran2003 import *
from fparser.common.readfortran import FortranStringReader
from pymbolic.primitives import (Sum, Product, Quotient, Power, Comparison, LogicalNot,
                                 LogicalAnd, LogicalOr)
from pathlib import Path

from loki.visitors import GenericVisitor
from loki.frontend.source import Source
from loki.frontend.util import inline_comments, cluster_comments, inline_pragmas
from loki.ir import (
    Comment, Declaration, Statement, Loop, Conditional, Allocation, Deallocation,
    TypeDef, Import, Intrinsic, Call
)
from loki.types import DataType, BaseType, DerivedType
from loki.expression import Variable, Literal, InlineCall, Array, RangeIndex, LiteralList
from loki.expression.operations import ParenthesisedAdd, ParenthesisedMul, ParenthesisedPow
from loki.logging import info, error, DEBUG
from loki.tools import timeit, as_tuple, flatten

__all__ = ['FParser2IR', 'parse_fparser_file', 'parse_fparser_ast']


def node_sublist(nodelist, starttype, endtype):
    """
    Extract a subset of nodes from a list that sits between marked
    start and end nodes.
    """
    sublist = []
    active = False
    for node in nodelist:
        if isinstance(node, endtype):
            active = False

        if active:
            sublist += [node]

        if isinstance(node, starttype):
            active = True
    return sublist


class FParser2IR(GenericVisitor):

    def __init__(self, typedefs=None, shape_map=None, type_map=None, cache=None):
        super(FParser2IR, self).__init__()
        self.typedefs = typedefs
        self.shape_map = shape_map
        self.type_map = type_map

        # Use provided symbol cache for variable generation
        self._cache = None  # cache

#    def Variable(self, *args, **kwargs):
#        """
#        Instantiate cached variable symbols from local symbol cache.
#        """
#        if self._cache is None:
#            return Variable(*args, **kwargs)
#        else:
#            return self._cache.Variable(*args, **kwargs)

    def visit(self, o, **kwargs):
        """
        Generic dispatch method that tries to generate meta-data from source.
        """
        source = kwargs.pop('source', None)
        if not isinstance(o, str) and o.item is not None:
            source = Source(lines=o.item.span)
        return super(FParser2IR, self).visit(o, source=source, **kwargs)

    def visit_Base(self, o, **kwargs):
        """
        Universal default for ``Base`` FParser-AST nodes
        """
        children = tuple(self.visit(c, **kwargs) for c in o.items if c is not None)
        if len(children) == 1:
            return children[0]  # Flatten hierarchy if possible
        else:
            return children if len(children) > 0 else None

    def visit_BlockBase(self, o, **kwargs):
        """
        Universal default for ``BlockBase`` FParser-AST nodes
        """
        children = tuple(self.visit(c, **kwargs) for c in o.content)
        children = tuple(c for c in children if c is not None)
        if len(children) == 1:
            return children[0]  # Flatten hierarchy if possible
        else:
            return children if len(children) > 0 else None

    def visit_Name(self, o, **kwargs):
        # This one is evil, as it is used flat in expressions,
        # forcing us to generate ``Variable`` objects, and in
        # declarations, where nonde of the metadata is available
        # at this low level!
        vname = o.tostr()
        dimensions = kwargs.get('dimensions', None)
        # Careful! Mind the many ways in which this can get called with
        # outside information (either in kwargs or maps stored on self).
        shape = kwargs.get('shape', None)
        if shape is None:
            shape = self.shape_map.get(vname, None) if self.shape_map else None
        dtype = kwargs.get('dtype', None)
        if dtype is None:
            dtype = self.type_map.get(vname, None) if self.type_map else None
        parent = kwargs.get('parent', None)

        # If a parent variable is given, try to infer type and shape
        if parent is not None and self.type_map is not None:
            parent_type = self.type_map.get(parent.name, None)
            if (parent_type is not None and isinstance(parent_type, DerivedType) \
                    and parent_type.variables is not None):
                typevar = [v for v in parent_type.variables
                           if v.name.lower() == vname.lower()][0]
                dtype = typevar.type
                if isinstance(typevar, Array):
                    shape = typevar.shape

        return Variable(name=vname, dimensions=dimensions, shape=shape, type=dtype, parent=parent)

    def visit_Char_Literal_Constant(self, o, **kwargs):
        return Literal(value=str(o.items[0]), kind=o.items[1]) 

    def visit_Int_Literal_Constant(self, o, **kwargs):
        return Literal(value=int(o.items[0]), kind=o.items[1])

    def visit_Real_Literal_Constant(self, o, **kwargs):
        return Literal(value=float(o.items[0]), kind=o.items[1])

    def visit_Logical_Literal_Constant(self, o, **kwargs):
        return Literal(value=o.items[0], type=DataType.BOOL)

    def visit_Attr_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Component_Attr_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Dimension_Attr_Spec(self, o, **kwargs):
        return self.visit(o.items[1])

    def visit_Component_Attr_Spec(self, o, **kwargs):
        return o.tostr()

    def visit_Attr_Spec(self, o, **kwargs):
        return o.tostr()

    def visit_Specification_Part(self, o, **kwargs):
        children = tuple(self.visit(c, **kwargs) for c in o.content)
        children = tuple(c for c in children if c is not None)
        return list(children)

    def visit_Use_Stmt(self, o, **kwargs):
        name = o.items[2].tostr()
        symbols = as_tuple(self.visit(s) for s in o.items[4].items)
        return Import(module=name, symbols=symbols)

    def visit_Include_Stmt(self, o, **kwargs):
        fname = o.items[0].tostr()
        return Import(module=fname, c_import=True)

    def visit_Implicit_Stmt(self, o, **kwargs):
        return Intrinsic(text='IMPLICIT %s' % o.items[0],
                         source=kwargs.get('source', None))

    def visit_Print_Stmt(self, o, **kwargs):
        return Intrinsic(text='PRINT %s' % (', '.join(str(i) for i in o.items)),
                         source=kwargs.get('source', None))

    def visit_Comment(self, o, **kwargs):
        return Comment(text=o.tostr())

    def visit_Entity_Decl(self, o, **kwargs):
        # Don't recurse here, as the node is a ``Name`` and will
        # generate a pre-cached ``Variable`` object otherwise!
        vname = o.items[0].tostr()
        dtype = kwargs.get('dtype', None)

        dims = get_child(o, Explicit_Shape_Spec_List)
        dims = get_child(o, Assumed_Shape_Spec_List) if dims is None else dims
        dimensions = self.visit(dims) if dims is not None else kwargs.get('dimensions', None)

        init = get_child(o, Initialization)
        initial = self.visit(init) if init is not None else None

        # We know that this is a declaration, so the ``dimensions``
        # here also define the shape of the variable symbol within the
        # currently cached context.
        return Variable(name=vname, type=dtype, dimensions=dimensions,
                        shape=dimensions, initial=initial)

    def visit_Component_Decl(self, o, **kwargs):
        dtype = kwargs.get('dtype', None)
        dims = get_child(o, Explicit_Shape_Spec_List)
        dims = get_child(o, Assumed_Shape_Spec_List) if dims is None else dims
        dims = get_child(o, Deferred_Shape_Spec_List) if dims is None else dims
        dimensions = self.visit(dims) if dims is not None else kwargs.get('dimensions', None)
        return self.visit(o.items[0], dimensions=dimensions, dtype=dtype, shape=dimensions)

    def visit_Entity_Decl_List(self, o, **kwargs):
        return as_tuple(self.visit(i, **kwargs) for i in as_tuple(o.items))

    def visit_Component_Decl_List(self, o, **kwargs):
        return as_tuple(self.visit(i, **kwargs) for i in as_tuple(o.items))

    def visit_Explicit_Shape_Spec(self, o, **kwargs):
        lower = None if o.items[0] is None else self.visit(o.items[0])
        upper = None if o.items[1] is None else self.visit(o.items[1])
        return RangeIndex(lower=lower, upper=upper, step=None)

    def visit_Explicit_Shape_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Assumed_Shape_Spec(self, o, **kwargs):
        lower = None if o.items[0] is None else self.visit(o.items[0])
        upper = None if o.items[1] is None else self.visit(o.items[1])
        return RangeIndex(lower=lower, upper=upper, step=None)

    def visit_Assumed_Shape_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Deferred_Shape_Spec(self, o, **kwargs):
        lower = None if o.items[0] is None else self.visit(o.items[0])
        upper = None if o.items[1] is None else self.visit(o.items[1])
        return RangeIndex(lower=lower, upper=upper, step=None)

    def visit_Deferred_Shape_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Allocation(self, o, **kwargs):
        dimensions = self.visit(o.items[1])
        return self.visit(o.items[0], dimensions=dimensions)

    def visit_Allocate_Shape_Spec(self, o, **kwargs):
        lower = None if o.items[0] is None else self.visit(o.items[0])
        upper = None if o.items[1] is None else self.visit(o.items[1])
        return RangeIndex(lower=lower, upper=upper, step=None)

    def visit_Allocate_Shape_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Allocate_Stmt(self, o, **kwargs):
        allocations = get_child(o, Allocation_List)
        variables = as_tuple(self.visit(a) for a in allocations.items)
        return Allocation(variables=variables)

    def visit_Deallocate_Stmt(self, o, **kwargs):
        deallocations = get_child(o, Allocate_Object_List)
        variables = as_tuple(self.visit(a) for a in deallocations.items)
        return Deallocation(variable=variables)

    def visit_Intrinsic_Type_Spec(self, o, **kwargs):
        dtype = o.items[0]
        kind = o.items[1].items[1].tostr() if o.items[1] is not None else None
        return dtype, kind

    def visit_Intrinsic_Name(self, o, **kwargs):
        return o.tostr()

    def visit_Initialization(self, o, **kwargs):
        return self.visit(o.items[1])

    def visit_Array_Constructor(self, o, **kwargs):
        values = self.visit(o.items[1])
        return LiteralList(values=values)

    def visit_Ac_Value_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Intrinsic_Function_Reference(self, o, **kwargs):
        name = self.visit(o.items[0])
        args = self.visit(o.items[1])
        kwarguments = {a[0].name: a[1] for a in args if isinstance(a, tuple)}
        arguments = as_tuple(a for a in args if not isinstance(a, tuple))
        return InlineCall(name, parameters=arguments, kw_parameters=kwarguments)

    def visit_Section_Subscript_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Subscript_Triplet(self, o, **kwargs):
        lower = None if o.items[0] is None else self.visit(o.items[0])
        upper = None if o.items[1] is None else self.visit(o.items[1])
        step = None if o.items[2] is None else self.visit(o.items[2])
        return RangeIndex(lower=lower, upper=upper, step=step)

    def visit_Actual_Arg_Spec_List(self, o, **kwargs):
        return as_tuple(self.visit(i) for i in o.items)

    def visit_Data_Ref(self, o, **kwargs):
        pname = o.items[0].tostr()
        v = Variable(name=pname)
        for i in o.items[1:-1]:
            # Careful not to propagate type or dims here
            v = self.visit(i, parent=v)
        # Attach types and dims to final leaf variable
        return self.visit(o.items[-1], parent=v, **kwargs)

    def visit_Part_Ref(self, o, **kwargs):
        name = o.items[0].tostr()
        args = as_tuple(self.visit(o.items[1]))
        if name.lower() in ['min', 'max', 'exp', 'sqrt', 'abs', 'log',
                            'selected_real_kind', 'allocated', 'present']:
            kwarguments = {k: a for k, a in args if isinstance(a, tuple)}
            arguments = as_tuple(a for a in args if not isinstance(a, tuple))
            return InlineCall(name, parameters=arguments, kw_parameters=kwarguments)
        else:
            shape = None
            dtype = None
            parent = kwargs.get('parent', None)

            if parent is not None and self.type_map is not None:
                parent_type = self.type_map.get(parent.name, None)
                if (parent_type is not None and isinstance(parent_type, DerivedType) \
                        and parent_type.variables is not None):
                    typevar = [v for v in parent_type.variables
                               if v.name.lower() == name.lower()][0]
                    dtype = typevar.type
                    if isinstance(typevar, Array):
                        shape = typevar.shape

            if shape is None:
                shape = self.shape_map.get(name, None) if self.shape_map else None
            if dtype is None:
                dtype = self.type_map.get(name, None) if self.type_map else None

            return Variable(name=name, dimensions=args, parent=parent, shape=shape, type=dtype)

    def visit_Array_Section(self, o, **kwargs):
        dimensions = as_tuple(self.visit(o.items[1]))
        return self.visit(o.items[0], dimensions=dimensions)

    def visit_Substring_Range(self, o, **kwargs):
        lower = None if o.items[0] is None else self.visit(o.items[0])
        upper = None if o.items[1] is None else self.visit(o.items[1])
        return RangeIndex(lower=lower, upper=upper)

    def visit_Type_Declaration_Stmt(self, o, **kwargs):
        # First, pick out parameters, including explicit DIMENSIONs
        attrs = as_tuple(self.visit(o.items[1])) if o.items[1] is not None else ()
        # Super-hacky, this fecking DIMENSION keyword will be my undoing one day!
        dimensions = [a for a in attrs if isinstance(a, tuple)]
        dimensions = None if len(dimensions) == 0 else dimensions[0]
        attrs = tuple(str(a).lower().strip() for a in attrs if isinstance(a, str))
        intent = None
        if 'intent(in)' in attrs:
            intent = 'in'
        elif 'intent(inout)' in attrs:
            intent = 'inout'
        elif 'intent(out)' in attrs:
            intent = 'out'

        # Next, figure out the type we're declararing
        dtype = None
        basetype_ast = get_child(o, Intrinsic_Type_Spec)
        if basetype_ast is not None:
            dtype, kind = self.visit(basetype_ast)
            dtype = BaseType(dtype, kind=kind, intent=intent,
                             parameter='parameter' in attrs, optional='optional' in attrs,
                             allocatable='allocatable' in attrs, pointer='pointer' in attrs)

        derived_type_ast = get_child(o, Declaration_Type_Spec)
        if derived_type_ast is not None:
            typename = derived_type_ast.items[1].tostr()
            # TODO: Insert variable information from stored TypeDef!
            if self.typedefs is not None and typename in self.typedefs:
                variables = self.typedefs[typename].variables
            else:
                variables = None
            dtype = DerivedType(name=typename, variables=variables, intent=intent,
                                allocatable='allocatable' in attrs,
                                pointer='pointer' in attrs, optional='optional' in attrs,
                                parameter='parameter' in attrs, target='target' in attrs)

        variables = self.visit(o.items[2], dtype=dtype, dimensions=dimensions)
        return Declaration(variables=flatten(variables), type=dtype, dimensions=dimensions)

    def visit_Derived_Type_Def(self, o, **kwargs):
        name = get_child(o, Derived_Type_Stmt).items[1].tostr()
        declarations = self.visit(get_child(o, Component_Part))
        return TypeDef(name=name, declarations=declarations)

    def visit_Component_Part(self, o, **kwargs):
        return as_tuple(self.visit(a) for a in o.content)

    def visit_Data_Component_Def_Stmt(self, o, **kwargs):
        # First, determine type attributes
        attrs = as_tuple(self.visit(o.items[1])) if o.items[1] is not None else ()
        # Super-hacky, this fecking DIMENSION keyword will be my undoing one day!
        dimensions = [a for a in attrs if isinstance(a, tuple)]
        dimensions = None if len(dimensions) == 0 else dimensions[0]
        attrs = tuple(str(a).lower().strip() for a in attrs if isinstance(a, str))
        intent = None
        if 'intent(in)' in attrs:
            intent = 'in'
        elif 'intent(inout)' in attrs:
            intent = 'inout'
        elif 'intent(out)' in attrs:
            intent = 'out'

        # Next, figure out the type we're declararing
        dtype = None
        basetype_ast = get_child(o, Intrinsic_Type_Spec)
        if basetype_ast is not None:
            dtype, kind = self.visit(basetype_ast)
            dtype = BaseType(dtype, kind=kind, intent=intent,
                             parameter='parameter' in attrs, optional='optional' in attrs,
                             allocatable='allocatable' in attrs, pointer='pointer' in attrs)

        derived_type_ast = get_child(o, Declaration_Type_Spec)
        if derived_type_ast is not None:
            typename = derived_type_ast.items[1].tostr()
            # TODO: Insert variable information from stored TypeDef!
            if self.typedefs is not None and typename in self.typedefs:
                variables = self.typedefs[typename].variables
            else:
                variables = None
            dtype = DerivedType(name=typename, variables=variables, intent=intent,
                                allocatable='allocatable' in attrs,
                                pointer='pointer' in attrs, optional='optional' in attrs,
                                parameter='parameter' in attrs, target='target' in attrs)

        variables = self.visit(o.items[2], dtype=dtype, dimensions=dimensions)
        # TODO: Deal with our Loki-specific dimension annotations
        return Declaration(variables=flatten(variables), type=dtype, dimensions=dimensions)

    def visit_Block_Nonlabel_Do_Construct(self, o, **kwargs):
        # Extract loop header and get stepping info
        # TODO: Will need to handle labeled ones too at some point
        dostmt = get_child(o, Nonlabel_Do_Stmt)
        variable, bounds = self.visit(dostmt)
        if len(bounds) == 2:
            # Ensure we always have a step size
            bounds += (None,)

        # Extract and process the loop body
        body_nodes = node_sublist(o.content, Nonlabel_Do_Stmt, End_Do_Stmt)
        body = as_tuple(self.visit(node) for node in body_nodes)

        return Loop(variable=variable, body=body, bounds=bounds)

    def visit_Nonlabel_Do_Stmt(self, o, **kwargs):
        variable, bounds = self.visit(o.items[1])
        return variable, bounds

    def visit_If_Construct(self, o, **kwargs):
        if_then = get_child(o, Fortran2003.If_Then_Stmt)
        conditions = as_tuple(self.visit(if_then))
        body_ast = node_sublist(o.content, Fortran2003.If_Then_Stmt, Fortran2003.Else_Stmt)
        else_ast = node_sublist(o.content, Fortran2003.Else_Stmt, Fortran2003.End_If_Stmt)
        # TODO: Multiple elif bodies..!
        bodies = as_tuple(self.visit(a) for a in as_tuple(body_ast))
        else_body = as_tuple(self.visit(a) for a in as_tuple(else_ast))
        return Conditional(conditions=conditions, bodies=bodies,
                           else_body=else_body, inline=if_then is None)

    def visit_If_Then_Stmt(self, o, **kwargs):
        return self.visit(o.items[0])

    def visit_Call_Stmt(self, o, **kwargs):
        name = o.items[0].tostr()
        args = self.visit(o.items[1])
        kwarguments = as_tuple(a for a in args if isinstance(a, tuple))
        arguments = as_tuple(a for a in args if not isinstance(a, tuple))
        return Call(name=name, arguments=arguments, kwarguments=kwarguments)

    def visit_Loop_Control(self, o, **kwargs):
        variable = self.visit(o.items[1][0])
        bounds = as_tuple(self.visit(a) for a in as_tuple(o.items[1][1]))
        return variable, bounds

    def visit_Assignment_Stmt(self, o, **kwargs):
        target = self.visit(o.items[0])
        expr = self.visit(o.items[2])
        return Statement(target=target, expr=expr)

    def visit_operation(self, op, exprs):
        """
        Construct expressions from individual operations, suppressing SymPy simplifications.
        """
        exprs = as_tuple(exprs)
        if op == '*':
            return Product(exprs)
        elif op == '/':
            return Quotient(numerator=exprs[0], denominator=exprs[1])
        elif op == '+':
            return Sum(exprs)
        elif op == '-':
            if len(exprs) > 1:
                # Binary minus
                return Sum((exprs[0], Product((-1, exprs[1]))))
            else:
                # Unary minus
                return Product((-1, exprs[0]))
        elif op == '**':
            return Power(base=exprs[0], exponent=exprs[1])
        elif op.lower() == '.and.':
            return LogicalAnd(exprs)
        elif op.lower() == '.or.':
            return LogicalOr(exprs)
        elif op == '==' or op.lower() == '.eq.':
            return Comparison(exprs[0], '==', exprs[1])
        elif op == '/=' or op.lower() == '.ne.':
            return Comparison(exprs[0], '!=', exprs[1])
        elif op == '>' or op.lower() == '.gt.':
            return Comparison(exprs[0], '>', exprs[1])
        elif op == '<' or op.lower() == '.lt.':
            return Comparison(exprs[0], '<', exprs[1])
        elif op == '>=' or op.lower() == '.ge.':
            return Comparison(exprs[0], '>=', exprs[1])
        elif op == '<=' or op.lower() == '.le.':
            return Comparison(exprs[0], '<=', exprs[1])
        elif op.lower() == '.not.':
            return LogicalNot(exprs[0])
        else:
            raise RuntimeError('FParser: Error parsing generic expression')

    def visit_Add_Operand(self, o, **kwargs):
        if len(o.items) > 2:
            exprs = [self.visit(o.items[0])]
            exprs += [self.visit(o.items[2])]
            return self.visit_operation(op=o.items[1], exprs=exprs)
        else:
            exprs = [self.visit(o.items[1])]
            return self.visit_operation(op=o.items[0], exprs=exprs)

    visit_Mult_Operand = visit_Add_Operand
    visit_And_Operand = visit_Add_Operand
    visit_Or_Operand = visit_Add_Operand
    visit_Equiv_Operand = visit_Add_Operand

    def visit_Level_2_Expr(self, o, **kwargs):
        e1 = self.visit(o.items[0])
        e2 = self.visit(o.items[2])
        return self.visit_operation(op=o.items[1], exprs=(e1, e2))

    def visit_Level_2_Unary_Expr(self, o, **kwargs):
        exprs = as_tuple(self.visit(o.items[1]))
        return self.visit_operation(op=o.items[0], exprs=exprs)

    visit_Level_4_Expr = visit_Level_2_Expr

    def visit_Parenthesis(self, o, **kwargs):
        expression = self.visit(o.items[1])
        if isinstance(expression, Sum):
            expression = ParenthesisedAdd(expression.children)
        if isinstance(expression, Product):
            expression = ParenthesisedMul(expression.children)
        if isinstance(expression, Power):
            expression = ParenthesisedPow(expression.base, expression.exponent)
        return expression


@timeit(log_level=DEBUG)
def parse_fparser_file(filename):
    """
    Generate an internal IR from file via the fparser AST.
    """
    filepath = Path(filename)
    with filepath.open('r') as f:
        fcode = f.read()

    # Remove ``#`` in front of include statements
    fcode = fcode.replace('#include', 'include')

    reader = FortranStringReader(fcode, ignore_comments=False)
    f2008_parser = ParserFactory().create(std='f2008')

    return f2008_parser(reader)  # , raw_source


@timeit(log_level=DEBUG)
def parse_fparser_ast(ast, typedefs=None, shape_map=None, type_map=None, cache=None):
    """
    Generate an internal IR from file via the fparser AST.
    """

    # Parse the raw FParser language AST into our internal IR
    ir = FParser2IR(typedefs=typedefs, shape_map=shape_map, type_map=type_map, cache=cache).visit(ast)

    # Perform soime minor sanitation tasks
    ir = inline_comments(ir)
    ir = cluster_comments(ir)
    ir = inline_pragmas(ir)

    return ir
