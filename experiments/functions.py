"""Byte-exact function ranges; parsing only, no target code execution."""
import ast
import textwrap


def _bindings(body, direct=True):
    result = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.append((node.name, node, direct))
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.extend((a.asname or a.name.split('.')[0], node, False) for a in node.names)
        if isinstance(node, ast.Delete):
            result.extend((n.id, node, False) for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name))
        if isinstance(node, (ast.With, ast.AsyncWith)):
            result.extend((n.id, node, False) for item in node.items if item.optional_vars
                          for n in ast.walk(item.optional_vars) if isinstance(n, ast.Name))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            result.extend((n.id, node, False) for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
        expressions = [v for _, v in ast.iter_fields(node) if isinstance(v, ast.expr)]
        if isinstance(node, (ast.Assign, ast.Delete)):
            expressions += list(node.targets)
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ('exec', 'eval', 'globals', 'locals') for expr in expressions for n in ast.walk(expr)):
            raise ValueError('dynamic scope not eligible')
        result.extend((n.target.id, node, False) for expr in expressions for n in ast.walk(expr)
                      if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name))
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                statements = [v for v in value if isinstance(v, ast.stmt)]
                result.extend(_bindings(statements, False))
                for handler in (v for v in value if isinstance(v, ast.ExceptHandler)):
                    if handler.name:
                        result.append((handler.name, handler, False))
                    result.extend(_bindings(handler.body, False))
                for case in (v for v in value if isinstance(v, ast.match_case)):
                    result.extend((n.name, node, False) for n in ast.walk(case.pattern)
                                  if isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name)
                    result.extend((n.rest, node, False) for n in ast.walk(case.pattern) if isinstance(n, ast.MatchMapping) and n.rest)
                    result.extend(_bindings(case.body, False))
            elif isinstance(value, ast.match_case):
                result.extend(_bindings(value.body, False))
    return result


def _offset(lines, line, column):
    return sum(len(x) for x in lines[:line-1]) + column


def locate_function(source, qualified_name):
    if not isinstance(source, str) or not isinstance(qualified_name, str):
        raise ValueError('text and qualified name required')
    tree = ast.parse(source)
    parts = qualified_name.split('.')
    if not parts or any(not p.isidentifier() for p in parts):
        raise ValueError('invalid qualified name')
    body, classes = tree.body, []
    for index, part in enumerate(parts):
        matches = [(n, direct) for name, n, direct in _bindings(body) if name == part]
        if len(matches) != 1 or not matches[0][1]:
            raise ValueError('ambiguous, conditional or shadowed target')
        node = matches[0][0]
        if index < len(parts)-1:
            if not isinstance(node, ast.ClassDef):
                raise ValueError('nested closure target not eligible')
            classes.append(node)
            body = node.body
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise ValueError('target must be a function or method')
    if any((isinstance(d, ast.Name) and d.id == 'overload') or
           (isinstance(d, ast.Attribute) and d.attr == 'overload') for d in node.decorator_list):
        raise ValueError('overload target not eligible')
    raw = source.encode('utf-8')
    lines = raw.splitlines(keepends=True)
    start = _offset(lines, node.lineno, node.col_offset)
    end = _offset(lines, node.end_lineno, node.end_col_offset)
    indent = lines[node.lineno-1][:node.col_offset]
    if indent.strip(b' \t'):
        raise ValueError('target definition must start its own line')
    code = textwrap.dedent((indent + raw[start:end]).decode('utf-8'))
    return {'node': node, 'tree': tree, 'start': start, 'end': end, 'indent': indent,
            'line': node.lineno, 'end_line': node.end_lineno, 'code': code,
            'newline': '\r\n' if lines[node.lineno-1].endswith(b'\r\n') else '\n',
            'classes': [{'name': c.name, 'bases': [ast.unparse(b) for b in c.bases]} for c in classes],
            'decorators': [ast.get_source_segment(source, d) for d in node.decorator_list]}


def replace_function(source, qualified_name, candidate):
    scope = locate_function(source, qualified_name)
    if not isinstance(candidate, str):
        raise ValueError('function proposal must be text')
    candidate = textwrap.dedent(candidate).strip('\r\n').rstrip()
    if not candidate.startswith(('def ', 'async def ')):
        raise ValueError('proposal starts at undecorated def')
    tree = ast.parse(candidate)
    if len(tree.body) != 1:
        raise ValueError('only one function definition allowed')
    old, new = scope['node'], tree.body[0]
    if (type(new) is not type(old) or new.name != old.name or new.decorator_list
            or ast.dump(new.args) != ast.dump(old.args)):
        # Return annotations are handled separately because they may be None.
        raise ValueError('function type, name, signature or decorators changed')
    if (ast.dump(new.returns) if new.returns else None) != (ast.dump(old.returns) if old.returns else None):
        raise ValueError('return annotation changed')
    if [ast.dump(n) for n in getattr(new, 'type_params', [])] != [ast.dump(n) for n in getattr(old, 'type_params', [])]:
        raise ValueError('type parameters changed')
    raw_candidate = candidate.encode('utf-8')
    if _offset(raw_candidate.splitlines(keepends=True), new.end_lineno, new.end_col_offset) != len(raw_candidate):
        raise ValueError('trailing statements or comments outside function')
    normalized = candidate.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    inserted = (scope['newline'] + scope['indent'].decode()).join(normalized).encode('utf-8')
    raw = source.encode('utf-8')
    result = (raw[:scope['start']] + inserted + raw[scope['end']:]).decode('utf-8')
    ast.parse(result)
    return result
