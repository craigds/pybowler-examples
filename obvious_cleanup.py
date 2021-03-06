#!/usr/bin/env python3
"""
Cleans up simple obvious messy stuff:

    not a == b
    --> a != b

    a == None
    --> a is None

    dict([(k, v) for k, v in x])
    --> {k: v for k, v in x}

    set([a for a in x])
    --> {a for a in x}
"""

import argparse
import re
import sys
from functools import wraps

from fissix.fixer_util import parenthesize
from fissix.pygram import python_symbols as syms

from bowler import Query, TOKEN, SYMBOL
from bowler.types import Leaf, Node, STARS

flags = {}


def kw(name, **kwargs):
    """
    A helper to produce keyword nodes
    """
    kwargs.setdefault('prefix', ' ')
    return Leaf(TOKEN.NAME, name, **kwargs)


OPERATOR_INVERSIONS = {
    '==': Leaf(TOKEN.NOTEQUAL, '!=', prefix=' '),
    '!=': Leaf(TOKEN.EQEQUAL, '==', prefix=' '),
    '<': Leaf(TOKEN.GREATEREQUAL, '>=', prefix=' '),
    '>': Leaf(TOKEN.LESSEQUAL, '<=', prefix=' '),
    '<=': Leaf(TOKEN.GREATER, '>', prefix=' '),
    '>=': Leaf(TOKEN.LESS, '<', prefix=' '),
    'in': Node(syms.comp_op, [kw('not'), kw('in')], prefix=' '),
    'not in': kw('in'),
    'is': Node(syms.comp_op, [kw('is'), kw('not')], prefix=' '),
    'is not': kw('is'),
}


def invert_operator(op):
    return OPERATOR_INVERSIONS[str(op).strip()].clone()


def simplify_not_operators(node, capture, arguments):
    """
    not a == b
        --> a != b

    not a != b
        --> a == b

    not a is not b
    a is b
    """
    # invert the operator
    op = capture['comparison'].children[1]
    op.replace(invert_operator(op))
    # remove the 'not'
    rest = node.children[1].clone()
    rest.prefix = node.prefix
    node.replace(rest)


def simplify_none_operand(node, capture, arguments):
    """
    a != None
        --> a is not None
    """
    op = capture['op'][0]
    print(op)
    if op.type == TOKEN.EQEQUAL:
        op.replace(kw('is'))
    else:
        op.replace(Node(syms.comp_op, [kw('is'), kw('not')]))


def make_dict_comprehension(node, capture, arguments):
    """
    dict([(k, v) for k, v in x])
        --> {k: v for k, v in x}

    PYTHON 2 NOTE:
    Where list comprehensions in python 2 set local-scope variables,
    dict comprehensions do not!
    So this may change the behaviour of your code in subtle ways.
    e.g.

        >>> a = 5
        >>> b = dict([(a, a) for a in (1, 2, 3)])
        >>> print(a)
        3
        >>> a = 5
        >>> b = {a: a for a in (1, 2, 3)}
        >>> print(a)
        5
    """
    kv = capture['kv']
    key = capture['k']
    value = capture['v']
    forloop = capture['forloop'][0]
    ifpart = capture.get('ifpart') or None

    forloop.type = syms.comp_for
    if ifpart:
        ifpart.type = syms.comp_if

    node.replace(
        Node(
            syms.atom,
            [
                Leaf(TOKEN.LBRACE, "{"),
                Node(
                    syms.dictsetmaker,
                    [
                        key.clone(),
                        Leaf(TOKEN.COLON, ":"),
                        value.clone(),
                        forloop.clone(),
                    ],
                    prefix=kv.parent.prefix,
                ),
                Leaf(TOKEN.RBRACE, "}", prefix=kv.parent.get_suffix()),
            ],
            prefix=node.prefix,
        )
    )


def make_set_comprehension(node, capture, arguments):
    arg = capture['arg']
    forloop = capture['forloop'][0]
    ifpart = capture.get('ifpart') or None

    forloop.type = syms.comp_for
    if ifpart:
        ifpart.type = syms.comp_if

    node.replace(
        Node(
            syms.atom,
            [
                Leaf(TOKEN.LBRACE, "{"),
                Node(
                    syms.dictsetmaker,
                    [arg.clone(), forloop.clone()],
                    prefix=arg.parent.prefix,
                ),
                Leaf(TOKEN.RBRACE, "}", prefix=arg.parent.get_suffix()),
            ],
            prefix=node.prefix,
        )
    )


def remove_extra_parentheses(node, capture, arguments):
    node = capture['outer']
    inner = capture['inner']

    if node.children[0].get_lineno() != node.children[2].get_lineno():
        # don't touch parentheses on separate lines. they make a lot of
        # things syntactically correct that otherwise wouldn't be.
        return

    if isinstance(inner, list):
        inner = inner[0]
    if capture.get('assignment_form') and inner.type == syms.testlist_gexp:
        # a = (b,)
        # a = (b, c)
        # a = (b for c in d)
        # removing the parentheses might be syntactically valid (in the first two forms)
        # but it's not normal/best practice.
        # so we don't.
        return
    newnode = inner.clone()

    newnode.prefix = node.prefix
    node.replace(newnode)


def main():
    parser = argparse.ArgumentParser(
        description="Converts x-unit style tests to be pytest-style where possible."
    )
    parser.add_argument(
        '--no-input',
        dest='interactive',
        default=True,
        action='store_false',
        help="Non-interactive mode",
    )
    parser.add_argument(
        '--no-write',
        dest='write',
        default=True,
        action='store_false',
        help="Don't write the changes to the source file, just output a diff to stdout",
    )
    parser.add_argument(
        '--debug',
        dest='debug',
        default=False,
        action='store_true',
        help="Spit out debugging information",
    )
    parser.add_argument(
        'files', nargs='+', help="The python source file(s) to operate on."
    )
    args = parser.parse_args()

    # No way to pass this to .modify() callables, so we just set it at module level
    flags['debug'] = args.debug

    query = (
        # Look for files in the current working directory
        Query(*args.files)
        # 'not a == b' --> 'a != b'
        .select(
            '''
            not_test<
                "not" comparison=comparison< any* >
            >
        '''
        )
        .modify(callback=simplify_not_operators)
        # 'a == None' --> 'a is None'
        .select(
            '''
            comparison=comparison<
                ( a=any op=( "==" | "!=" ) none="None" )
                | ( none="None" op=( "==" | "!=" ) a=any )
            >
            '''
        )
        .modify(callback=simplify_none_operand)
        # dict([(a, b) for (a, b) in x])
        # dict((a, b) for (a, b) in x)
        # dict(((a, b) for (a, b) in x))
        # --> {a: b for (a, b) in x}
        .select(
            """
            power< "dict" trailer< '(' (
                atom< "[" listmaker< {kv} {forloop} > "]" >
                | argument< {kv} {forloop} >
                | atom< "(" testlist_gexp< {kv} {forloop} > ")" >
            ) ')' > >
            """.format(
                forloop='''forloop=(
                    comp_for< any* "in" any [ ifpart=comp_if< any* > ] >
                )''',
                kv='''
                    kv=atom< "(" testlist_gexp< k=any "," v=any > ")" >
                ''',
            )
        )
        .modify(callback=make_dict_comprehension)
        # set([a for a in x])
        # set((a for a in x))
        # set(a for a in x)
        # --> {a for a in x}
        #
        # TODO
        # set([x, y, z])
        # set((x, y, z))
        # --> {x, y, z}
        .select(
            """
            power< "set" trailer< '(' (
                atom< "[" listmaker< arg=any {forloop} > "]" >
                | argument< arg=any {forloop} >
                | atom< "(" testlist_gexp< arg=any {forloop} > ")" >
            ) ')' > >
            """.format(
                forloop='''forloop=(
                    comp_for< any* "in" any [ ifpart=comp_if< any* > ] >
                )'''
            )
        )
        .modify(callback=make_set_comprehension)
        # (a)
        # --> a
        # func((x for x in y))
        # --> func(x for x in y)
        .select(
            """
                (
                    assignment_form=expr_stmt<
                        any
                        "="
                        outer=atom<
                            "("
                            inner=any
                            ")"
                        >
                    >
                    |
                    outer=atom<
                        "("
                        inner=(NAME | NUMBER | STRING | factor | atom< "(" any ")" >)
                        ")"
                    >
                    |
                    any<
                        "("
                        outer=atom<
                            "("
                            inner=testlist_gexp<
                                any
                                comp_for
                            >
                            ")"
                        >
                        ")"
                    >
                )
            """
        )
        .modify(callback=remove_extra_parentheses)
        # Actually run all of the above.
        .execute(
            # interactive diff implies write (for the bits the user says 'y' to)
            interactive=(args.interactive and args.write),
            write=args.write,
        )
    )


if __name__ == '__main__':
    main()
