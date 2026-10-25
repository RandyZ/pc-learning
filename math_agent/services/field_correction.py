"""Narrow, hash-bound corrections for V2 variable declaration shape errors.

A model still supplies the assumptions; the server never invents 'real' or any
stronger assumption. Only identified variables fields are replaceable. Unknown
validation shapes use the caller's existing bounded handling, never a guessed
path or an expanded patch permission.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import re

from pydantic import ValidationError
from ..domain.v2 import digest

ASSUMPTIONS = ("real", "positive", "nonnegative", "nonzero")
SYMBOL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")


def pointer(path):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in path)


@dataclass(frozen=True)
class VariableFields:
    base_sha256: str
    # Trusted tuples created from local validation; never from model paths.
    fields: tuple[tuple[tuple, tuple[str, ...]], ...]

    def schema(self):
        fields = {}
        for path, symbols in self.fields:
            mapping = {'type': 'object', 'additionalProperties': False,
                       'required': list(symbols),
                       'properties': {s: {'type': 'string', 'enum': list(ASSUMPTIONS)} for s in symbols}}
            fields[pointer(path)] = {'anyOf': [mapping, {'type': 'null'}],
                'description': 'Replace only this variables field. Null means unresolved and stops correction.'}
        return {'type': 'object', 'additionalProperties': False,
                'required': ['base_sha256', 'replacements'], 'properties': {
                    'base_sha256': {'type': 'string', 'enum': [self.base_sha256]},
                    'replacements': {'type': 'object', 'additionalProperties': False,
                                     'required': list(fields), 'properties': fields}}}

    def context(self, original):
        """Relevant math context, not all boards or a new full-output request."""
        self._check_base(original)
        items = []
        for path, symbols in self.fields:
            _, qid, _, index, _ = path
            question = original['questions'][qid]
            items.append({'path': pointer(path), 'error': 'variables must be a name-to-assumption object',
                          'symbols_to_preserve': list(symbols),
                          'operation': copy.deepcopy(question['computations'][index])})
        qids = dict.fromkeys(path[1] for path, _ in self.fields)
        return {'base_sha256': self.base_sha256, 'fields': items,
                'proof_context': {qid: copy.deepcopy(original['questions'][qid]['proof']) for qid in qids}}

    def _check_base(self, original):
        if digest(original) != self.base_sha256:
            raise ValueError('field correction base changed; refusing stale replacement')

    def apply(self, original, patch):
        self._check_base(original)
        if not isinstance(patch, dict) or set(patch) != {'base_sha256', 'replacements'}:
            raise ValueError('field correction requires only base_sha256 and replacements, not a full Draft')
        if patch['base_sha256'] != self.base_sha256:
            raise ValueError('field correction hash mismatch')
        values = patch['replacements']
        if not isinstance(values, dict) or set(values) != {pointer(p) for p, _ in self.fields}:
            raise ValueError('field correction must cover exactly the authorized paths')
        result = copy.deepcopy(original)
        for path, symbols in self.fields:
            value = values[pointer(path)]
            if value is None:
                raise ValueError('variable assumptions remain unresolved at ' + pointer(path))
            if not isinstance(value, dict) or set(value) != set(symbols):
                raise ValueError('field correction cannot add/remove/rename variables')
            if any(type(v) is not str or v not in ASSUMPTIONS for v in value.values()):
                raise ValueError('field correction contains an unsupported assumption')
            parent = result
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = copy.deepcopy(value)
        return result


def variable_fields(original, error):
    """Only an exhaustive set of variables-array errors can authorize this patch."""
    if not isinstance(error, ValidationError) or not isinstance(original, dict):
        return None
    errors = error.errors(include_url=False, include_input=False, include_context=False)
    if not errors or len(errors) > 64:
        return None
    fields = []
    for item in errors:
        path = item['loc']
        if (item['type'] != 'dict_type' or len(path) != 5 or path[0] != 'questions'
                or not isinstance(path[1], str) or path[2] != 'computations'
                or type(path[3]) is not int or path[3] < 0 or path[4] != 'variables'):
            return None
        try:
            value = original
            for part in path:
                value = value[part]
        except (KeyError, IndexError, TypeError):
            return None
        if (not isinstance(value, list) or len(value) > 4
                or any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in value)
                or len(set(value)) != len(value)):
            return None
        fields.append((tuple(path), tuple(value)))
    if len({p for p, _ in fields}) != len(fields):
        return None
    return VariableFields(digest(original), tuple(fields))


def error_locations(error):
    """Bounded path/code metadata only: never include rejected mathematical values."""
    if not isinstance(error, ValidationError):
        return []
    return [{'path': '.'.join(map(str, e['loc']))[:240], 'code': e['type']}
            for e in error.errors(include_url=False, include_input=False, include_context=False)[:64]]
