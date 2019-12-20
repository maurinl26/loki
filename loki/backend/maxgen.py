from functools import reduce

from loki.backend import CCodegen
from loki.expression.symbol_types import Array, LokiStringifyMapper
from loki.ir import Import, Declaration
from loki.tools import chunks
from loki.types import DataType
from loki.visitors import Visitor, FindNodes, Transformer

__all__ = ['maxjgen', 'maxjmanagergen', 'maxjcgen', 'MaxjCodegen', 'MaxjCodeMapper',
           'MaxjManagerCodegen', 'MaxjCCodegen']


def maxj_local_type(_type):
    if _type.dtype == DataType.LOGICAL:
        return 'boolean'
    elif _type.dtype == DataType.INTEGER:
        return 'int'
    elif _type.dtype == DataType.REAL:
        if str(_type.kind) in ['real32']:
            return 'float'
        else:
            return 'double'
    else:
        raise ValueError(str(_type))


def maxj_dfevar_type(_type):
    if _type.dtype == DataType.LOGICAL:
        return 'dfeBool()'
    elif _type.dtype == DataType.INTEGER:
        return 'dfeInt(32)'
    elif _type.dtype == DataType.REAL:
        if str(_type.kind) in ['real32']:
            return 'dfeFloat(8, 24)'
        else:
            return 'dfeFloat(11, 53)'
    else:
        raise ValueError(str(_type))


class MaxjCodeMapper(LokiStringifyMapper):

    def __init__(self, constant_mapper=None):
        super(MaxjCodeMapper, self).__init__(constant_mapper)

    def map_scalar(self, expr, *args, **kwargs):
        # TODO: Big hack, this is completely agnostic to whether value or address is to be assigned
        ptr = '*' if expr.type and expr.type.pointer else ''
        if expr.parent is not None:
            parent = self.parenthesize(self.rec(expr.parent, *args, **kwargs))
            return self.format('%s%s.%s', ptr, parent, expr.basename)
        else:
            return self.format('%s%s', ptr, expr.name)

    def map_array(self, expr, *args, **kwargs):
        dims = [self.rec(d, *args, **kwargs) for d in expr.dimensions]
        dims = ''.join(['[%s]' % d for d in dims if len(d) > 0])
        if expr.parent is not None:
            parent = self.parenthesize(self.rec(expr.parent, *args, **kwargs))
            return self.format('%s.%s%s', parent, expr.basename, dims)
        else:
            return self.format('%s%s', expr.basename, dims)

    def map_range_index(self, expr, *args, **kwargs):
        lower = self.rec(expr.lower, *args, **kwargs) if expr.lower else ''
        upper = self.rec(expr.upper, *args, **kwargs) if expr.upper else ''
        if expr.step:
            return '(%s - %s + 1) / %s' % (upper, lower, self.rec(expr.step, *args, **kwargs))
        else:
            return '(%s - %s + 1)' % (upper, lower)


class MaxjCodegen(Visitor):
    """
    Tree visitor to generate Maxeler maxj kernel code from IR.
    """

    def __init__(self, depth=0, linewidth=90, chunking=6):
        super(MaxjCodegen, self).__init__()
        self.linewidth = linewidth
        self.chunking = chunking
        self._depth = depth
        self._maxjsymgen = MaxjCodeMapper()

    @property
    def indent(self):
        return '  ' * self._depth

    def segment(self, arguments, chunking=None):
        chunking = chunking or self.chunking
        delim = ',\n%s  ' % self.indent
        args = list(chunks(list(arguments), chunking))
        return delim.join(', '.join(c) for c in args)

    def type_and_stream(self, v, is_input=True):
        """
        Builds the string representation of the nested parameterized type for vectors and scalars
        and the matching initialization function or output stream.
        """
        base_type = self.visit(v.type)
        L = len(v.dimensions) if isinstance(v, Array) else 0

        # Build nested parameterized type
        types = ['DFEVar'] + ['DFEVector<%s>'] * L
        types = [reduce(lambda p, n: n % p, types[:i]) for i in range(1, L+2)]

        # Deduce matching type constructor
        init_templates = ['new DFEVectorType<%s>(%s, %s)'] * L
        inits = [base_type]
        for i in range(L):
            inits += [init_templates[i] % (types[i], inits[i],
                                           self._maxjsymgen(v.dimensions[-(i+1)]))]

        if is_input:
            if v.type.intent is not None and v.type.intent.lower() == 'in':
                # Determine matching initialization routine...
                stream_name = 'input' if isinstance(v, Array) or v.type.dfestream else 'scalarInput'
                stream = 'io.%s("%s", %s)' % (stream_name, v.name, inits[-1])
            elif v.initial is not None:
                # ...or assign a given initial value...
                stream = v.initial.name
            else:
                # ...or create an empty instance
                stream = '%s.newInstance(this)' % inits[-1]

        else:
            # Matching outflow statement
            if v.type.intent is not None and v.type.intent.lower() in ('inout', 'out'):
                sname = 'output' if isinstance(v, Array) or v.type.dfestream else 'scalarOutput'
                stream = 'io.{0}("{1}", {1}, {2})'.format(sname, v.name, inits[-1])
            else:
                stream = None

        return types[-1], stream

    def visit_Node(self, o):
        return self.indent + '// <%s>' % o.__class__.__name__

    def visit_tuple(self, o):
        return '\n'.join([self.visit(i) for i in o])

    visit_list = visit_tuple

    def visit_Subroutine(self, o):
        # Re-generate variable declarations
        o._externalize()

        package = 'package %s;\n\n' % o.name

        # Some boilerplate imports...
        imports = 'import com.maxeler.maxcompiler.v2.kernelcompiler.Kernel;\n'
        imports += 'import com.maxeler.maxcompiler.v2.kernelcompiler.KernelParameters;\n'
        imports += 'import com.maxeler.maxcompiler.v2.kernelcompiler.types.base.DFEVar;\n'
        imports += 'import com.maxeler.maxcompiler.v2.kernelcompiler.types.composite.DFEVector;\n'
        imports += 'import com.maxeler.maxcompiler.v2.kernelcompiler.types.composite.DFEVectorType;\n'
        imports += self.visit(FindNodes(Import).visit(o.spec))

        # Standard Kernel definitions
        header = 'class %sKernel extends Kernel {\n\n' % o.name
        self._depth += 1
        header += '%s%sKernel(KernelParameters parameters) {\n' % (self.indent, o.name)
        self._depth += 1
        header += self.indent + 'super(parameters);\n'

        # Generate declarations for local variables
        local_vars = [v for v in o.variables if v not in o.arguments]
        types = ['DFEVar' if v.type.dfevar else self.visit(v.type) for v in local_vars]
        names = [self._maxjsymgen(v) for v in local_vars]
        inits = [' = %s' % self._maxjsymgen(v.initial) if v.initial is not None else ''
                 for v in local_vars]
        spec = ['\n']
        spec += ['%s %s%s;\n' % vals for vals in zip(types, names, inits)]
        spec = self.indent.join(spec)

        # Remove any declarations for variables that are not arguments
        decl_map = {}
        for d in FindNodes(Declaration).visit(o.spec):
            if any([v in local_vars for v in d.variables]):
                decl_map[d] = None
        o.spec = Transformer(decl_map).visit(o.spec)

        # Generate remaining declarations
        spec = self.visit(o.spec) + spec

        # Remove pointer type from scalar arguments
        decl_map = {}
        for d in FindNodes(Declaration).visit(o.spec):
            if d.type.pointer:
                new_type = d.type
                new_type.pointer = False
                decl_map[d] = d.clone(type=new_type)
        o.spec = Transformer(decl_map).visit(o.spec)

        # Generate body
        body = self.visit(o.body)

        # Insert outflow statements for output variables
        outflow = [self.type_and_stream(v, is_input=False)
                   for v in o.arguments if v.type.intent.lower() in ('inout', 'out')]
        outflow = '\n'.join(['%s%s;' % (self.indent, a[1]) for a in outflow])

        self._depth -= 1
        footer = '\n%s}\n}' % self.indent
        self._depth -= 1

        return package + imports + '\n' + header + spec + '\n' + body + '\n' + outflow + footer

    def visit_Section(self, o):
        return self.visit(o.body) + '\n'

    def visit_Declaration(self, o):
        # Ignore parameters
        if o.type.parameter:
            return ''

        comment = '  %s' % self.visit(o.comment) if o.comment is not None else ''

        # Determine the underlying data type and initialization value
        vtype, vinit = zip(*[self.type_and_stream(v, is_input=True) for v in o.variables])
        variables = ['%s%s %s = %s;' % (self.indent, t, v.name, i)
                     for v, t, i in zip(o.variables, vtype, vinit)]
        return self.segment(variables) + comment

    def visit_SymbolType(self, o):
        if o.dtype == DataType.DERIVED_TYPE:
            return 'DFEStructType %s' % o.name
        elif o.dfevar:
            return maxj_dfevar_type(o)
        else:
            return maxj_local_type(o)

    def visit_TypeDef(self, o):
        self._depth += 1
        decls = self.visit(o.declarations)
        self._depth -= 1
        return 'DFEStructType %s {\n%s\n} ;' % (o.name, decls)

    def visit_Comment(self, o):
        text = o._source.string if o.text is None else o.text
        return self.indent + text.replace('!', '//')

    def visit_CommentBlock(self, o):
        comments = [self.visit(c) for c in o.comments]
        return '\n'.join(comments)

    def visit_Statement(self, o):
        if isinstance(o.target, Array):
            stmt = '%s <== %s;\n' % (self._maxjsymgen(o.target),
                                     self._maxjsymgen(o.expr))
        else:
            stmt = '%s = %s;\n' % (self._maxjsymgen(o.target),
                                   self._maxjsymgen(o.expr))
        comment = '  %s' % self.visit(o.comment) if o.comment is not None else ''
        return self.indent + stmt + comment

    def visit_ConditionalStatement(self, o):
        stmt = '%s = %s ? %s : %s;' % (self._maxjsymgen(o.target), self._maxjsymgen(o.condition),
                                       self._maxjsymgen(o.expr), self._maxjsymgen(o.else_expr))
        return self.indent + stmt

    def visit_Intrinsic(self, o):
        return o.text

    def visit_Loop(self, o):
        self._depth += 1
        body = self.visit(o.body)
        self._depth -= 1
        header = self.indent + 'for ({0} = {1}; {0} <= {2}; {0} += {3}) '
        header = header.format(o.variable.name, o.bounds[0], o.bounds[1],
                               o.bounds[2] or 1)
        return header + '{\n' + body + '\n' + self.indent + '}\n'


class MaxjManagerCodegen(object):

    def __init__(self, depth=0, linewidth=90, chunking=6):
        self.linewidth = linewidth
        self.chunking = chunking
        self._depth = depth

    @property
    def indent(self):
        return '  ' * self._depth

    def gen(self, o):
        # Standard boilerplate header
        imports = 'package %s;\n\n' % o.name
        imports += 'import com.maxeler.maxcompiler.v2.build.EngineParameters;\n'
        imports += 'import com.maxeler.maxcompiler.v2.kernelcompiler.Kernel;\n'
        imports += 'import com.maxeler.maxcompiler.v2.managers.custom.blocks.KernelBlock;\n'
        # imports += 'import com.maxeler.maxcompiler.v2.managers.engine_interfaces.CPUTypes;\n'
        # imports += 'import com.maxeler.maxcompiler.v2.managers.engine_interfaces.EngineInterface;\n'
        imports += 'import com.maxeler.platform.max5.manager.MAX5CManager;\n'
        imports += '\n'

        # Class definitions
        header = 'public class %sManager extends MAX5CManager {\n\n' % o.name
        self._depth += 1
        header += self.indent + 'public static final String kernelName = "%sKernel";\n\n' % o.name
        header += self.indent + 'public %sManager(EngineParameters params) {\n' % o.name
        self._depth += 1

        # Making the kernel known
        body = [self.indent + 'super(params);\n']
        body += ['Kernel kernel = new %sKernel(makeKernelParameters(kernelName));\n' % o.name]
        body += ['KernelBlock kernelBlock = addKernel(kernel);\n']

        # Insert in/out streams
        in_vars = [v for v in o.arguments
                   if isinstance(v, Array) and v.type.intent.lower() == 'in']
        out_vars = [v for v in o.arguments
                    if isinstance(v, Array) and v.type.intent.lower() in ('inout', 'out')]
        body += ['\n']
        body += ['kernelBlock.getInput("{0}") <== addStreamFromCPU("{0}");\n'.format(v.name)
                 for v in in_vars]
        body += ['addStreamToCPU("{0}") <== kernelBlock.getOutput("{0}");\n'.format(v.name)
                 for v in out_vars]

        # Specify default values for interface parameters
        # body += ['\n']
        # body += ['EngineInterface ei = new EngineInterface("kernel");\n']
        # body += ['ei.setTicks(kernelName, 1000);\n']  # TODO: Put a useful value here!

        # Specify sizes of streams
        # stream_template = 'ei.setStream("{0}", {1}, {2} * {1}.sizeInBytes());\n'
        # in_sizes = [', '.join([str(d) for d in v.dimensions]) for v in in_vars]
        # out_sizes = [', '.join([str(d) for d in v.dimensions]) for v in out_vars]
        # body += ['\n']
        # body += [stream_template.format(v.name, v.type.dtype.maxjManagertype, s)
        #          for v, s in zip(in_vars, in_sizes)]
        # body += [stream_template.format(v.name, v.type.dtype.maxjManagertype, s)
        #          for v, s in zip(out_vars, out_sizes)]

        # body += ['\n']
        # body += ['createSLiCinterface(ei);\n']
        body = self.indent.join(body)

        # Writing the main for maxJavaRun
        self._depth -= 1
        main_header = self.indent + '}\n\n'
        main_header += self.indent + 'public static void main(String[] args) {\n'
        self._depth += 1

        main_body = [self.indent + 'EngineParameters params = new EngineParameters(args);\n']
        main_body += ['MAX5CManager manager = new %sManager(params);\n' % o.name]
        main_body += ['manager.build();\n']
        main_body = self.indent.join(main_body)

        self._depth -= 1
        footer = self.indent + '}\n'
        self._depth -= 1
        footer += self.indent + '}'

        return imports + header + body + main_header + main_body + footer


class MaxjCCodegen(CCodegen):

    def visit_Call(self, o):
        # astr = [csymgen(a) for a in o.arguments]
        # astr = ['*%s' % arg.name if not arg.is_Array and arg.type.pointer else arg.name
        #         for arg in o.arguments]
        astr = [arg.name for arg in o.arguments]
        return '%s%s(%s);' % (self.indent, o.name, ', '.join(astr))


def maxjgen(ir):
    """
    Generate Maxeler maxj kernel code from one or many IR objects/trees.
    """
    return MaxjCodegen().visit(ir)


def maxjmanagergen(ir):
    """
    Generate Maxeler maxj manager for the given IR objects/trees.
    """
    return MaxjManagerCodegen().gen(ir)


def maxjcgen(ir):
    """
    Generate a C routine that wraps the call to the Maxeler kernel.
    """
    return MaxjCCodegen().visit(ir)
