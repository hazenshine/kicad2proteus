import re, sys

TOK = re.compile(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+')

def parse(text):
    stack = [[]]
    for m in TOK.finditer(text):
        t = m.group(0)
        if t == '(':
            stack.append([])
        elif t == ')':
            x = stack.pop()
            stack[-1].append(x)
        elif t.startswith('"'):
            stack[-1].append(t[1:-1].replace('\\"', '"').replace('\\\\', '\\'))
        else:
            stack[-1].append(t)
    return stack[0][0]

def find(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]

def first(node, key):
    r = find(node, key)
    return r[0] if r else None

def val(node, key, default=None):
    f = first(node, key)
    return f[1] if f and len(f) > 1 else default
