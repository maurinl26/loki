from itertools import groupby
from enum import IntEnum
from pathlib import Path
import codecs

from loki.visitors import Visitor, NestedTransformer
from loki.ir import (Statement, CallStatement, Comment, CommentBlock, Declaration, Pragma, Loop,
                     Intrinsic)
from loki.frontend.source import Source
from loki.types import DataType, SymbolType
from loki.expression import Literal, Variable
from loki.tools import as_tuple
from loki.logging import warning

__all__ = ['Frontend', 'OFP', 'OMNI', 'FP', 'inline_comments', 'cluster_comments',
           'inline_pragmas', 'process_dimension_pragmas', 'read_file']


class Frontend(IntEnum):
    OMNI = 1
    OFP = 2
    FP = 3

    def __str__(self):
        return self.name.lower()  # pylint: disable=no-member


OMNI = Frontend.OMNI
OFP = Frontend.OFP
FP = Frontend.FP  # The STFC FParser


class SequenceFinder(Visitor):
    """
    Utility visitor that finds repeated nodes of the same type in
    lists/tuples within a given tree.
    """

    def __init__(self, node_type):
        super(SequenceFinder, self).__init__()
        self.node_type = node_type

    @classmethod
    def default_retval(cls):
        return []

    def visit_tuple(self, o, **kwargs):
        groups = []
        for c in o:
            # First recurse...
            subgroups = self.visit(c)
            if subgroups is not None and len(subgroups) > 0:
                groups += subgroups
        for t, group in groupby(o, type):
            # ... then add new groups
            g = tuple(group)
            if t is self.node_type and len(g) > 1:
                groups.append(g)
        return groups

    visit_list = visit_tuple


class PatternFinder(Visitor):
    """
    Utility visitor that finds a pattern of nodes given as tuple/list
    of types within a given tree.
    """

    def __init__(self, pattern):
        super(PatternFinder, self).__init__()
        self.pattern = pattern

    @classmethod
    def default_retval(cls):
        return []

    @staticmethod
    def match_indices(pattern, sequence):
        """ Return indices of matched patterns in sequence. """
        matches = []
        for i, elem in enumerate(sequence):
            if elem == pattern[0]:
                if tuple(sequence[i:i+len(pattern)]) == tuple(pattern):
                    matches.append(i)
        return matches

    def visit_tuple(self, o, **kwargs):
        matches = []
        for c in o:
            # First recurse...
            submatches = self.visit(c)
            if submatches is not None and len(submatches) > 0:
                matches += submatches
        types = list(map(type, o))
        idx = self.match_indices(self.pattern, types)
        for i in idx:
            matches.append(o[i:i+len(self.pattern)])
        return matches

    visit_list = visit_tuple


def inline_comments(ir):
    """
    Identify inline comments and merge them onto statements
    """
    pairs = PatternFinder(pattern=(Statement, Comment)).visit(ir)
    pairs += PatternFinder(pattern=(Declaration, Comment)).visit(ir)
    mapper = {}
    for pair in pairs:
        # Comment is in-line and can be merged
        # Note, we need to re-create the statement node
        # so that Transformers don't throw away the changes.
        if pair[0]._source and pair[1]._source:
            if pair[1]._source.lines[0] == pair[0]._source.lines[1]:
                mapper[pair[0]] = pair[0]._rebuild(comment=pair[1])
                mapper[pair[1]] = None  # Mark for deletion
    return NestedTransformer(mapper).visit(ir)


def cluster_comments(ir):
    """
    Cluster comments into comment blocks
    """
    comment_mapper = {}
    comment_groups = SequenceFinder(node_type=Comment).visit(ir)
    for comments in comment_groups:
        # Build a CommentBlock and map it to first comment
        # and map remaining comments to None for removal
        if all(c._source is not None for c in comments):
            if all(c._source.string is not None for c in comments):
                string = ''.join(c._source.string for c in comments)
            else:
                string = None
            lines = (comments[0]._source.lines[0], comments[-1]._source.lines[1])
            source = Source(lines=lines, string=string, file=comments[0]._source.file,
                            label=comments[0]._source.label)
        else:
            source = None
        block = CommentBlock(comments, source=source)
        comment_mapper[comments[0]] = block
        for c in comments[1:]:
            comment_mapper[c] = None
    return NestedTransformer(comment_mapper).visit(ir)


def inline_pragmas(ir):
    """
    Find pragmas and merge them onto declarations and subroutine calls

    Note: Pragmas in derived types are already associated with the
    declaration due to way we parse derived types.
    """
    pairs = PatternFinder(pattern=(Pragma, Declaration)).visit(ir)
    pairs += PatternFinder(pattern=(Pragma, CallStatement)).visit(ir)
    pairs += PatternFinder(pattern=(Pragma, Loop)).visit(ir)
    mapper = {}
    for pair in pairs:
        # Merge pragma with declaration and delete
        mapper[pair[0]] = None  # Mark for deletion
        mapper[pair[1]] = pair[1]._rebuild(pragma=pair[0])
    return NestedTransformer(mapper).visit(ir)


def inline_labels(ir):
    """
    Find labels and merge them onto the following node.

    Note: This is currently only required for OMNI and OFP frontends which
    has labels as nodes next to the corresponding statement without
    any connection between both.
    """
    pairs = PatternFinder(pattern=(Comment, Statement)).visit(ir)
    pairs += PatternFinder(pattern=(Comment, Intrinsic)).visit(ir)
    mapper = {}
    for pair in pairs:
        if pair[0]._source and pair[0].text == '__STATEMENT_LABEL__':
            if pair[1]._source and pair[1]._source.lines[0] == pair[0]._source.lines[1]:
                mapper[pair[0]] = None  # Mark for deletion
                mapper[pair[1]] = pair[1]._rebuild(source=pair[0]._source)
    return NestedTransformer(mapper).visit(ir)


def process_dimension_pragmas(typedef):
    """
    Process any '!$loki dimension' pragmas to override deferred dimensions
    """
    pragmas = {p._source.lines[0]: p for p in typedef.pragmas}
    for decl in typedef.declarations:
        # Map pragmas by declaration line, not var line
        if decl._source.lines[0]-1 in pragmas:
            pragma = pragmas[decl._source.lines[0]-1]
            for v in decl.variables:
                if pragma.keyword == 'loki' and pragma.content.startswith('dimension'):
                    # Found dimension override for variable
                    dims = pragma.content.split('dimension(')[-1]
                    dims = dims.split(')')[0].split(',')
                    dims = [d.strip() for d in dims]
                    shape = []
                    for d in dims:
                        if d.isnumeric():
                            shape += [Literal(value=int(d), type=DataType.INTEGER)]
                        else:
                            _type = SymbolType(DataType.INTEGER)
                            shape += [Variable(name=d, scope=typedef.symbols, type=_type)]
                    v.type = v.type.clone(shape=as_tuple(shape))


def read_file(file_path):
    """
    Reads a file and returns the content as string.

    This convenience function is provided to catch read errors due to bad
    character encodings in the file. It skips over these characters and
    prints a warning for the first occurence of such a character.
    """
    filepath = Path(file_path)
    try:
        with filepath.open('r') as f:
            source = f.read()
    except UnicodeDecodeError as excinfo:
        warning('Skipping bad character in input file "%s": %s',
                str(filepath), str(excinfo))
        kwargs = {'mode': 'r', 'encoding': 'utf-8', 'errors': 'ignore'}
        with codecs.open(filepath, **kwargs) as f:
            source = f.read()
    return source
