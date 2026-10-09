"""Static time/space complexity analysis for Python code (AST based).

The analysis is derived from the *structure* of the program (loop nesting,
halving loops, recursion shape, sorting, auxiliary allocations) rather than
from keywords, so the numbers are explainable and repeatable.
n always means "size of the input".
"""
import ast

# A cost is (exp, deg, log):  exp -> base of exponential (0 none, 99 = factorial)
#                             deg -> power of n,  log -> power of log n
C1 = (0, 0, 0)
CLOG = (0, 0, 1)
CN = (0, 1, 0)
CNLOGN = (0, 1, 1)

_SUP = {2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹"}


def _mul(a, b):
    return (max(a[0], b[0]), a[1] + b[1], a[2] + b[2])


def _max(a, b):
    return a if a >= b else b


def fmt(c):
    exp, deg, lg = c
    if exp == 99:
        return "O(n!)"
    if exp:
        return "O(2ⁿ)" if exp == 2 else "O(%dⁿ)" % exp
    parts = []
    if deg == 0.5:
        parts.append("√n")
    elif deg == 1:
        parts.append("n")
    elif deg and float(deg).is_integer() and int(deg) in _SUP:
        parts.append("n" + _SUP[int(deg)])
    elif deg:
        parts.append("n^%s" % deg)
    if lg == 1:
        parts.append("log n")
    elif lg:
        parts.append("log%s n" % _SUP.get(int(lg), "^%d" % lg))
    return "O(%s)" % (" ".join(parts) if parts else "1")


_HASH_HINTS = ("visited", "seen", "memo", "cache", "lookup", "freq", "count", "hash", "table", "mapping")
_N_FUNCS = {"sum", "min", "max", "any", "all", "list", "tuple", "set", "frozenset", "dict", "sorted", "reversed",
            "join", "count", "index", "find", "remove", "copy", "reverse", "extend", "split", "strip", "replace",
            "upper", "lower", "heapify", "deepcopy", "counter", "bytes", "bytearray", "str_join"}
_ALLOC_FUNCS = {"list", "tuple", "set", "frozenset", "dict", "sorted", "copy", "split", "deepcopy", "counter"}
_GROW = {"append", "add", "extend", "insert", "appendleft", "push", "heappush", "setdefault", "update", "put"}
_HEAP = {"heappush", "heappop", "heappushpop", "heapreplace"}


def _is_const_num(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)


def _names(node):
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}


def _has_halving(node):
    """Expression divides/shifts by a constant >= 2 (or takes a modulo -> Euclid style)."""
    for x in ast.walk(node):
        if isinstance(x, ast.BinOp):
            if isinstance(x.op, (ast.FloorDiv, ast.Div, ast.RShift)) and _is_const_num(x.right) and x.right.value >= 1:
                return True
            if isinstance(x.op, ast.Mod):
                return True
    return False


class _Ctx:
    def __init__(self, fn=None):
        self.fn = fn
        self.name = fn.name if fn is not None else None
        self.types = {}
        self.params = set()
        self.halved = set()
        self.graph = False
        self.uses_heap = False
        if fn is not None:
            args = fn.args
            for a in list(args.args) + list(args.kwonlyargs) + list(getattr(args, "posonlyargs", [])):
                self.params.add(a.arg)
            for d, a in zip(reversed(args.defaults), reversed(args.args)):
                if isinstance(d, (ast.Dict, ast.Set)) or (isinstance(d, ast.Call) and getattr(d.func, "id", "") in ("dict", "set")):
                    self.types[a.arg] = "hash"
        for x in ast.walk(fn if fn is not None else ast.Module(body=[], type_ignores=[])):
            if isinstance(x, ast.Assign) and isinstance(x.targets[0], ast.Name) and _has_halving(x.value):
                self.halved.add(x.targets[0].id)

    def is_hash(self, name):
        t = self.types.get(name)
        if t == "hash":
            return True
        if t:
            return False
        low = name.lower()
        return any(h in low for h in _HASH_HINTS)


class Analyzer:
    def __init__(self, tree):
        self.tree = tree
        self.funcs = {}
        self.methods = {}
        self.classes = {}
        self._cache = {}
        self._stack = []
        self.features = {"recursion": None, "sort": False, "max_loop": 0, "memo": False, "graph": False,
                         "swap": False, "const_loop": False, "log_loop": False, "alloc": False, "early_exit": False}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.methods.setdefault(node.name, []).append(m)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.setdefault(node.name, node)

    # ---------- iteration counts ----------
    def iter_cost(self, it):
        if isinstance(it, ast.Call) and getattr(it.func, "id", "") == "range":
            for a in it.args:
                for x in ast.walk(a):
                    if isinstance(x, ast.BinOp) and isinstance(x.op, ast.Pow) and isinstance(x.right, ast.Constant) and x.right.value == 0.5:
                        return (0, 0.5, 0)
                    if isinstance(x, ast.Call) and getattr(x.func, "attr", getattr(x.func, "id", "")) in ("sqrt", "isqrt"):
                        return (0, 0.5, 0)
            if all(_is_const_num(a) or (isinstance(a, ast.UnaryOp) and _is_const_num(a.operand)) for a in it.args):
                return C1
            return CN
        if isinstance(it, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict)):
            return C1 if not (isinstance(it, ast.Constant) and isinstance(it.value, str) and len(it.value) > 20) else CN
        return CN

    def while_is_log(self, st):
        test_names = _names(st.test)
        body = list(ast.walk(ast.Module(body=st.body, type_ignores=[])))
        for x in body:
            if isinstance(x, ast.AugAssign) and isinstance(x.target, ast.Name) and x.target.id in test_names:
                if isinstance(x.op, (ast.FloorDiv, ast.Div, ast.RShift)) and _is_const_num(x.value) and x.value.value >= 2:
                    return True
                if isinstance(x.op, (ast.Mult, ast.LShift)) and _is_const_num(x.value) and x.value.value >= 2:
                    return True
            if isinstance(x, ast.Assign):
                tg = x.targets[0]
                if isinstance(tg, ast.Name) and tg.id in test_names and isinstance(x.value, ast.BinOp):
                    v = x.value
                    if isinstance(v.op, (ast.FloorDiv, ast.Div, ast.RShift)) and _is_const_num(v.right) and v.right.value >= 2:
                        return True
                    if isinstance(v.op, ast.Mult) and any(_is_const_num(s) and s.value >= 2 for s in (v.left, v.right)) \
                            and tg.id in _names(v):
                        return True
                if isinstance(tg, ast.Tuple) and any(isinstance(e, ast.Name) and e.id in test_names for e in tg.elts) \
                        and any(isinstance(b, ast.BinOp) and isinstance(b.op, ast.Mod) for b in ast.walk(x.value)):
                    return True  # Euclid style
        # binary search: mid = (lo+hi)//2 and lo/hi moved from mid
        mids = set()
        for x in body:
            if isinstance(x, ast.Assign) and isinstance(x.targets[0], ast.Name) and _has_halving(x.value) \
                    and not any(isinstance(b, ast.Mod) for b in ast.walk(x.value)):
                mids.add(x.targets[0].id)
        if mids and len(test_names) >= 2:
            for x in body:
                if isinstance(x, ast.Assign) and isinstance(x.targets[0], ast.Name) and x.targets[0].id in test_names \
                        and (_names(x.value) & mids):
                    return True
        return False

    # ---------- expressions: (time, space) ----------
    def ex(self, n, ctx, mult):
        if n is None or isinstance(n, (ast.Constant, ast.Name)):
            return C1, C1
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            it = C1
            t_pre = C1
            t_in = C1
            s_in = C1
            for g in n.generators:
                it = _mul(it, self.iter_cost(g.iter))
                ti, _ = self.ex(g.iter, ctx, mult)
                t_pre = _max(t_pre, ti)
                for c in g.ifs:
                    t_in = _max(t_in, self.ex(c, ctx, mult)[0])
            elts = [n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt]
            for e in elts:
                te, se = self.ex(e, ctx, mult)
                t_in, s_in = _max(t_in, te), _max(s_in, se)
            if it >= CN:
                self.features["max_loop"] = max(self.features["max_loop"], int(it[1]) or 1)
            s = C1 if isinstance(n, ast.GeneratorExp) else _mul(it, s_in)
            return _max(t_pre, _mul(it, t_in)), s
        if isinstance(n, ast.Call):
            return self.call(n, ctx, mult)
        if isinstance(n, ast.Subscript):
            t, s = self.ex(n.value, ctx, mult)
            sl = n.slice
            if isinstance(sl, ast.Slice):
                bounds = [b for b in (sl.lower, sl.upper, sl.step) if b is not None]
                if not bounds or not all(_is_const_num(b) or (isinstance(b, ast.UnaryOp) and _is_const_num(b.operand)) for b in bounds) \
                        or any(b is None for b in (sl.lower, sl.upper)) and not all(_is_const_num(b) for b in bounds):
                    return _max(t, CN), _max(s, CN)
                return t, s
            ti, si = self.ex(sl, ctx, mult)
            return _max(t, ti), _max(s, si)
        if isinstance(n, ast.Compare):
            t, s = C1, C1
            left = n.left
            for op, comp in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    lin = True
                    if isinstance(comp, (ast.Set, ast.Dict, ast.Tuple, ast.List, ast.Constant)):
                        lin = False
                    elif isinstance(comp, ast.Call) and getattr(comp.func, "id", "") == "range":
                        lin = False
                    elif isinstance(comp, ast.Name) and ctx.is_hash(comp.id):
                        lin = False
                    elif isinstance(comp, ast.Attribute) and any(h in comp.attr.lower() for h in _HASH_HINTS):
                        lin = False
                    if lin:
                        t = _max(t, CN)
                left = comp
            for ch in [n.left] + n.comparators:
                tc, sc = self.ex(ch, ctx, mult)
                t, s = _max(t, tc), _max(s, sc)
            return t, s
        if isinstance(n, ast.BinOp):
            t, s = C1, C1
            for ch in (n.left, n.right):
                tc, sc = self.ex(ch, ctx, mult)
                t, s = _max(t, tc), _max(s, sc)
            lst = lambda o: isinstance(o, (ast.List, ast.ListComp)) or (isinstance(o, ast.Name) and ctx.types.get(o.id) == "list") \
                or (isinstance(o, ast.Subscript) and isinstance(o.slice, ast.Slice))
            if isinstance(n.op, ast.Add) and lst(n.left) and lst(n.right):
                return _max(t, CN), _max(s, CN)
            if isinstance(n.op, ast.Mult) and (isinstance(n.left, ast.List) or isinstance(n.right, ast.List)):
                other = n.right if isinstance(n.left, ast.List) else n.left
                size = C1 if _is_const_num(other) else CN
                return _max(t, size), _max(s, size)
            return t, s
        if isinstance(n, ast.Lambda):
            return C1, C1
        t, s = C1, C1
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, ast.expr):
                tc, sc = self.ex(ch, ctx, mult)
                t, s = _max(t, tc), _max(s, sc)
        return t, s

    def call(self, n, ctx, mult):
        t, s = C1, C1
        for a in list(n.args) + [k.value for k in n.keywords]:
            ta, sa = self.ex(a, ctx, mult)
            t, s = _max(t, ta), _max(s, sa)
        f = n.func
        if isinstance(f, ast.Attribute):
            self_t, _ = self.ex(f.value, ctx, mult) if not isinstance(f.value, ast.Name) else (C1, C1)
            t = _max(t, self_t)
            fname = f.attr
            recv = f.value
        else:
            fname = f.id if isinstance(f, ast.Name) else ""
            recv = None
        low = fname.lower()
        # user-defined function / constructor
        target = None
        if fname == ctx.name:
            return t, s
        if fname in self.classes:
            inits = [m for m in self.methods.get(fname, []) if m.name == "__init__"]
            target = inits[0] if inits else None
            s = _max(s, mult)
            self.features["alloc"] = True
        elif fname in self.funcs and (recv is None or (isinstance(recv, ast.Name) and recv.id == "self") or
                                      not (fname in _N_FUNCS or fname in _GROW)):
            target = self.funcs[fname]
        if target is not None:
            info = self.info(target)
            if info is not None:
                t, s = _max(t, info["time"]), _max(s, info["space"])
            return t, s
        # builtins / library methods
        if low in _HEAP:
            ctx.uses_heap = True
            t = _max(t, CLOG)
            s = _max(s, mult)
            return t, s
        if low in ("bisect", "bisect_left", "bisect_right", "insort"):
            return _max(t, CLOG), s
        if low == "sorted" or (low == "sort" and recv is not None):
            self.features["sort"] = True
            t = _max(t, CNLOGN)
            if low == "sorted":
                s = _max(s, CN)
            return t, s
        if low in ("sqrt", "pow", "abs", "len", "print", "input", "int", "float", "round", "range", "enumerate", "zip",
                   "map", "filter", "isinstance", "ord", "chr", "str", "bool", "format", "type", "iter", "next"):
            if low == "range" or low in ("enumerate", "zip", "map", "filter"):
                return t, s
            return t, s
        if low in ("pop", "insert") and recv is not None:
            first = n.args[0] if n.args else None
            if low == "insert" or (first is not None and _is_const_num(first) and first.value == 0):
                t = _max(t, CN)
                if low == "insert":
                    s = _max(s, mult)
            return t, s
        if low in ("min", "max") and len(n.args) >= 2 and recv is None:
            return t, s
        if low in _N_FUNCS:
            arg = n.args[0] if n.args else None
            small = arg is not None and isinstance(arg, (ast.Constant,)) or (low in ("list", "tuple", "set", "dict", "copy") and not n.args)
            if not small:
                t = _max(t, CN)
                if low in _ALLOC_FUNCS and (n.args or low in ("copy", "split", "deepcopy")):
                    s = _max(s, CN)
                    self.features["alloc"] = True
                if low == "extend":
                    s = _max(s, _mul(mult, CN))
            return t, s
        if low in _GROW:
            s = _max(s, mult)
            self.features["alloc"] = True
        return t, s

    # ---------- statements ----------
    def block(self, body, ctx, mult, depth=0):
        t, s = C1, C1
        for st in body:
            ts, ss = self.stmt(st, ctx, mult, depth)
            t, s = _max(t, ts), _max(s, ss)
        return t, s

    def _track(self, st, ctx, mult):
        tgt = st.targets[0] if isinstance(st, ast.Assign) else st.target
        val = st.value
        if isinstance(tgt, ast.Name) and val is not None:
            v = val
            if isinstance(v, (ast.List, ast.ListComp)) or (isinstance(v, ast.BinOp) and isinstance(v.left, ast.List)):
                ctx.types[tgt.id] = "list"
            elif isinstance(v, (ast.Dict, ast.Set, ast.DictComp, ast.SetComp)) or \
                    (isinstance(v, ast.Call) and getattr(v.func, "id", getattr(v.func, "attr", "")) in
                     ("dict", "set", "defaultdict", "Counter", "OrderedDict")):
                ctx.types[tgt.id] = "hash"
            elif isinstance(v, ast.Constant) and isinstance(v.value, str):
                ctx.types[tgt.id] = "str"

    def stmt(self, st, ctx, mult, depth):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom,
                           ast.Pass, ast.Break, ast.Continue, ast.Global, ast.Nonlocal)):
            return C1, C1
        if isinstance(st, ast.For) or isinstance(st, ast.AsyncFor):
            it = self.iter_cost(st.iter)
            if it == C1:
                self.features["const_loop"] = True
            if it >= CN:
                self.features["max_loop"] = max(self.features["max_loop"], depth + 1)
            tpre, spre = self.ex(st.iter, ctx, mult)
            tb, sb = self.block(st.body, ctx, _mul(mult, it), depth + 1)
            to, so = self.block(st.orelse, ctx, mult, depth)
            if self._early_exit(st.body):
                self.features["early_exit"] = True
            if self._has_swap(st.body):
                self.features["swap"] = True
            return _max(_max(tpre, _mul(it, tb)), to), _max(_max(spre, sb), so)
        if isinstance(st, ast.While):
            log = self.while_is_log(st)
            it = CLOG if log else CN
            if log:
                self.features["log_loop"] = True
            else:
                self.features["max_loop"] = max(self.features["max_loop"], depth + 1)
            if self._early_exit(st.body):
                self.features["early_exit"] = True
            if self._has_swap(st.body):
                self.features["swap"] = True
            tt, _ = self.ex(st.test, ctx, mult)
            # graph traversal: while queue/stack with inner loop over neighbours and a visited set
            if not log and self._graph_loop(st, ctx):
                ctx.graph = True
                self.features["graph"] = True
                tb, sb = self.block(st.body, ctx, mult, depth + 1)
                return (CNLOGN if ctx.uses_heap else CN), _max(sb, CN)
            tb, sb = self.block(st.body, ctx, _mul(mult, it), depth + 1)
            to, so = self.block(st.orelse, ctx, mult, depth)
            return _max(_max(tt, _mul(it, tb)), to), _max(sb, so)
        if isinstance(st, ast.If):
            tt, ss = self.ex(st.test, ctx, mult)
            tb, sb = self.block(st.body, ctx, mult, depth)
            to, so = self.block(st.orelse, ctx, mult, depth)
            return _max(tt, _max(tb, to)), _max(ss, _max(sb, so))
        if isinstance(st, (ast.Try,)):
            t, s = self.block(st.body, ctx, mult, depth)
            for h in st.handlers:
                th, sh = self.block(h.body, ctx, mult, depth)
                t, s = _max(t, th), _max(s, sh)
            tf, sf = self.block(st.finalbody + st.orelse, ctx, mult, depth)
            return _max(t, tf), _max(s, sf)
        if isinstance(st, (ast.With, ast.AsyncWith)):
            return self.block(st.body, ctx, mult, depth)
        if isinstance(st, ast.Assign):
            self._track(st, ctx, mult)
            t, s = self.ex(st.value, ctx, mult)
            # string building: r = c + r copies the string every time
            tg0 = st.targets[0]
            if isinstance(tg0, ast.Name) and ctx.types.get(tg0.id) == "str" and isinstance(st.value, ast.BinOp) \
                    and isinstance(st.value.op, ast.Add) and tg0.id in _names(st.value):
                s = _max(s, mult)
                if isinstance(st.value.right, ast.Name) and st.value.right.id == tg0.id:
                    t = _max(t, CN)
            # d[key] = value on a local dict/set -> grows with each execution
            for tg in st.targets:
                if isinstance(tg, ast.Subscript) and isinstance(tg.value, ast.Name):
                    nm = tg.value.id
                    if ctx.types.get(nm) == "hash" or (nm not in ctx.params and ctx.is_hash(nm)):
                        s = _max(s, mult)
                        self.features["alloc"] = True
            return t, s
        if isinstance(st, ast.AnnAssign):
            self._track(st, ctx, mult)
            return self.ex(st.value, ctx, mult)
        if isinstance(st, ast.AugAssign):
            t, s = self.ex(st.value, ctx, mult)
            if isinstance(st.target, ast.Name) and isinstance(st.op, ast.Add):
                ty = ctx.types.get(st.target.id)
                if ty in ("str", "list"):
                    s = _max(s, mult)
                    self.features["alloc"] = True
                    if ty == "list" and not isinstance(st.value, ast.List):
                        t = _max(t, CN)
            if isinstance(st.target, ast.Subscript) and isinstance(st.target.value, ast.Name) \
                    and ctx.is_hash(st.target.value.id):
                s = _max(s, mult)
            return t, s
        t, s = C1, C1
        for ch in ast.iter_child_nodes(st):
            if isinstance(ch, ast.expr):
                tc, sc = self.ex(ch, ctx, mult)
                t, s = _max(t, tc), _max(s, sc)
        return t, s

    # ---------- helpers on structure ----------
    @staticmethod
    def _early_exit(body):
        for x in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(x, ast.If):
                for y in ast.walk(ast.Module(body=x.body, type_ignores=[])):
                    if isinstance(y, (ast.Return, ast.Break)):
                        return True
        return False

    @staticmethod
    def _has_swap(body):
        for x in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(x, ast.Assign) and isinstance(x.targets[0], ast.Tuple) and len(x.targets[0].elts) == 2 \
                    and all(isinstance(e, ast.Subscript) for e in x.targets[0].elts):
                return True
        return False

    def _graph_loop(self, st, ctx):
        names = {n.lower() for n in _names(st)}
        if not any("visited" in n or "seen" in n for n in names | {n.lower() for n in ctx.params}):
            return False
        for x in ast.walk(ast.Module(body=st.body, type_ignores=[])):
            if isinstance(x, ast.For):
                it = x.iter
                txt = ast.dump(it).lower()
                if isinstance(it, (ast.Subscript, ast.Attribute)) or "graph" in txt or "adj" in txt or "neighbor" in txt \
                        or (isinstance(it, ast.Call) and getattr(it.func, "attr", "") in ("get", "items", "neighbors")):
                    return True
        return False

    # ---------- recursion ----------
    def _self_calls(self, fn):
        out = []
        for x in ast.walk(fn):
            if isinstance(x, ast.Call):
                f = x.func
                if (isinstance(f, ast.Name) and f.id == fn.name) or \
                        (isinstance(f, ast.Attribute) and f.attr == fn.name and isinstance(f.value, ast.Name) and f.value.id == "self"):
                    out.append(x)
        return out

    def _count(self, body, name):
        """max number of self calls on one execution path; second value: any inside a loop"""
        k = 0
        looped = False
        for st in body:
            if isinstance(st, (ast.For, ast.While, ast.AsyncFor)):
                c, _ = self._count(st.body, name)
                if c:
                    looped = True
                    k += c
                k += self._expr_calls(st, name, skip_body=True)
            elif isinstance(st, ast.If):
                c1, l1 = self._count(st.body, name)
                c2, l2 = self._count(st.orelse, name)
                k += self._expr_calls(st.test, name) + max(c1, c2)
                looped = looped or l1 or l2
            elif isinstance(st, (ast.Try, ast.With)):
                c, l = self._count(st.body, name)
                k += c
                looped = looped or l
            elif isinstance(st, (ast.FunctionDef, ast.ClassDef)):
                continue
            else:
                k += self._expr_calls(st, name)
        return k, looped

    def _expr_calls(self, node, name, skip_body=False):
        if isinstance(node, ast.IfExp):
            return self._expr_calls(node.test, name) + max(self._expr_calls(node.body, name), self._expr_calls(node.orelse, name))
        c = 0
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                c += 1
        for ch in ast.iter_child_nodes(node):
            if skip_body and ch in getattr(node, "body", []):
                continue
            if isinstance(ch, (ast.stmt, ast.expr)):
                c += self._expr_calls(ch, name)
        return c

    def _arg_kind(self, fn, calls, ctx):
        kinds = set()
        for c in calls:
            for a in list(c.args) + [k.value for k in c.keywords]:
                if _has_halving(a) or (_names(a) & ctx.halved):
                    kinds.add("halve")
                    continue
                if isinstance(a, ast.Subscript) and isinstance(a.slice, ast.Slice) and a.slice.lower is None and a.slice.upper is not None \
                        and (_names(a.slice.upper) & ctx.halved):
                    kinds.add("halve")
                    continue
                if isinstance(a, (ast.Attribute,)) or (isinstance(a, ast.Subscript) and not isinstance(a.slice, ast.Slice)):
                    kinds.add("tree")
                    continue
                if any(isinstance(x, ast.BinOp) and isinstance(x.op, ast.Sub) for x in ast.walk(a)) or \
                        (isinstance(a, ast.Subscript) and isinstance(a.slice, ast.Slice)):
                    kinds.add("dec")
                    continue
                if any(isinstance(x, ast.BinOp) and isinstance(x.op, ast.Add) for x in ast.walk(a)):
                    kinds.add("inc")
                    continue
                kinds.add("other")
        for k in ("halve", "tree", "dec", "inc", "other"):
            if k in kinds:
                return k
        return "dec"

    def info(self, fn):
        key = id(fn)
        if key in self._cache:
            return self._cache[key]
        if key in self._stack:
            return None
        self._stack.append(key)
        ctx = _Ctx(fn)
        work, alloc = self.block(fn.body, ctx, C1)
        res = {"time": work, "space": alloc, "note": None, "recursive": False, "ctx": ctx}
        calls = self._self_calls(fn)
        if calls:
            res["recursive"] = True
            k, looped = self._count(fn.body, fn.name)
            k = max(k, 1)
            kind = self._arg_kind(fn, calls, ctx)
            memo = any(isinstance(d, ast.Name) and d.id in ("lru_cache", "cache") or
                       isinstance(d, ast.Call) and getattr(d.func, "id", getattr(d.func, "attr", "")) in ("lru_cache", "cache") or
                       isinstance(d, ast.Attribute) and d.attr in ("lru_cache", "cache")
                       for d in fn.decorator_list)
            if not memo:
                names = {x.id.lower() for x in ast.walk(fn) if isinstance(x, ast.Name)} | {a.lower() for a in ctx.params}
                memo = any(m in nm for nm in names for m in ("memo", "cache")) and \
                    any(isinstance(x, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in x.ops) for x in ast.walk(fn))
            visited = any("visited" in a.lower() or "seen" in a.lower() for a in
                          ({x.id for x in ast.walk(fn) if isinstance(x, ast.Name)} | set(ctx.params)))
            frame = _max(alloc, C1)
            if kind == "other" and k >= 2 and work >= CN:
                kind = "dc"
            if kind == "other":
                kind = "dec"
            if kind == "inc":
                kind = "dec"
            if memo:
                t = _mul(CN, _max(work, C1))
                sp = _max(CN, frame)
                note = "memoised recursion (each subproblem solved once)"
                self.features["memo"] = True
            elif kind == "tree" and visited:
                t, sp, note = (CNLOGN if ctx.uses_heap else CN), CN, "graph traversal (each vertex/edge once)"
                self.features["graph"] = True
            elif kind == "tree":
                t, sp, note = _mul(CN, _max(work, C1)), CN, "tree traversal (each node once)"
                res["space_label"] = "O(h)  (h = tree height; O(n) worst case)"
            elif kind == "halve":
                if work >= CN:
                    t = _mul(work, CLOG) if k >= 2 else work
                    if k >= 2 and work == CN:
                        t = CNLOGN
                else:
                    t = _max(CN, C1) if k >= 2 else _mul(work, CLOG)
                    if k >= 2:
                        t = CN
                sp = frame if frame >= CN else _mul(frame, CLOG)
                note = "problem size halves on every call"
            elif kind == "dc":
                t = _mul(work, CLOG)
                sp = frame if frame >= CN else CLOG
                note = "divide and conquer"
                res["dc"] = True
            else:  # decrement
                if looped:
                    t, note = (99, 0, 0), "recursive call inside a loop explores every ordering"
                    sp = _mul(CN, frame)
                elif k >= 2:
                    t, note = (k, 0, 0), "%d recursive calls per call on an input only slightly smaller" % k
                    sp = _mul(CN, frame)
                else:
                    t, note = _mul(CN, work), "one recursive call on a smaller input"
                    sp = _mul(CN, frame)
            res["time"], res["space"], res["note"], res["kind"] = t, sp, note, ("memo" if memo else kind)
            res["k"] = k
            self.features["recursion"] = res["kind"]
        self._stack.pop()
        self._cache[key] = res
        return res


# ---------------------------------------------------------------- labelling
_NAME_RULES = [
    (("binary_search", "bsearch", "binarysearch"), "Binary Search", "Searching"),
    (("linear_search", "linearsearch", "sequential_search"), "Linear Search", "Searching"),
    (("bubble",), "Bubble Sort", "Sorting"),
    (("selection_sort", "selectionsort"), "Selection Sort", "Sorting"),
    (("insertion_sort", "insertionsort"), "Insertion Sort", "Sorting"),
    (("merge_sort", "mergesort"), "Merge Sort", "Sorting"),
    (("quick_sort", "quicksort"), "Quick Sort", "Sorting"),
    (("heap_sort", "heapsort"), "Heap Sort", "Sorting"),
    (("dijkstra",), "Dijkstra's Shortest Path", "Graph Algorithm"),
    (("bfs", "breadth_first"), "Breadth-First Search", "Graph Algorithm"),
    (("dfs", "depth_first"), "Depth-First Search", "Graph Algorithm"),
    (("knapsack",), "Knapsack", "Dynamic Programming"),
    (("fibonacci", "fib"), "Fibonacci", "Recursion"),
    (("factorial",), "Factorial", "Math"),
    (("is_prime", "prime", "sieve"), "Prime Numbers", "Math"),
    (("gcd",), "Greatest Common Divisor", "Math"),
    (("palindrome",), "Palindrome Check", "Strings"),
    (("permutation",), "Permutations", "Recursion"),
    (("hanoi",), "Tower of Hanoi", "Recursion"),
]


def _label(a, tree, top, funcs, classes, rec_kind):
    names = [f.lower() for f in funcs] + [c.lower() for c in classes]
    blob = " ".join(names)
    f = a.features
    label = cat = None
    for keys, lab, ct in _NAME_RULES:
        if any(k in blob for k in keys):
            label, cat = lab, ct
            break
    if classes:
        meths = " ".join(m.name.lower() for c in classes.values() for m in a.methods.get(c.name, []))
        if "push" in meths and "pop" in meths:
            return "Stack", "Data Structure"
        if "enqueue" in meths or "dequeue" in meths:
            return "Queue", "Data Structure"
        attrs = " ".join(x.attr.lower() for x in ast.walk(tree) if isinstance(x, ast.Attribute))
        if "left" in attrs and "right" in attrs:
            return "Binary Tree", "Data Structure"
        if "next" in attrs:
            return "Linked List", "Data Structure"
        return "Custom Class (OOP)", "Data Structure"
    if label:
        if label == "Fibonacci":
            if f["memo"]:
                label = "Fibonacci (memoised)"
            elif f["recursion"]:
                label = "Fibonacci (recursive)"
            else:
                label = "Fibonacci (iterative)"
        return label, cat
    if f["graph"]:
        return "Graph Traversal", "Graph Algorithm"
    if f["log_loop"] and not f["recursion"] and f["max_loop"] == 0:
        return "Logarithmic Loop (halving)", "Searching / Math"
    if f["swap"] and f["max_loop"] >= 2:
        return "Sorting (swap based)", "Sorting"
    if f["sort"]:
        return "Sorting with sorted()/sort()", "Sorting"
    if f["memo"]:
        return "Memoised Recursion", "Dynamic Programming"
    if f["recursion"] == "tree":
        return "Tree Traversal", "Recursion"
    if f["recursion"]:
        return "Recursive Function", "Recursion"
    if top >= (0, 3, 0):
        return "Triple Nested Loops", "Iteration"
    if top >= (0, 2, 0):
        return "Nested Loops", "Iteration"
    if top > C1:
        return ("Single Loop (linear scan)" if f["max_loop"] <= 1 else "Loop-based Program"), "Iteration"
    return "Basic Python Program", "Basics"


def _explain(a, time_c, space_c, rec_kind, note):
    f = a.features
    bits = []
    if note:
        bits.append(note[0].upper() + note[1:])
    if f["max_loop"] >= 2 and not f["recursion"]:
        bits.append("%d nested loops over the input" % f["max_loop"])
    elif f["max_loop"] == 1 and not f["recursion"]:
        bits.append("one pass over the input")
    if f["log_loop"]:
        bits.append("a loop that halves (or doubles) its range each step")
    if f["sort"]:
        bits.append("a sort costs n log n")
    if not bits and time_c == C1:
        if f["const_loop"]:
            bits.append("loops run a fixed number of times (constant bounds), so the work doesn't grow with the input")
        else:
            bits.append("only fixed-size operations, no loops or recursion over the input")
    return "; ".join(bits).capitalize() + "."


def analyze(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"algorithm": "Unknown", "category": "General", "time_complexity": "Unavailable (fix syntax error first)",
                "space_complexity": "Unavailable", "best_case": "–", "worst_case": "–", "key_operations": "–",
                "explanation": "The code has a syntax error, so complexity can't be analysed.", "confidence": "low"}
    a = Analyzer(tree)
    # top-level script
    ctx = _Ctx(None)
    top_body = [s for s in tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    t, s = a.block(top_body, ctx, C1)
    # functions never called anywhere are analysed as the program's entry points
    called = set()
    for x in ast.walk(tree):
        if isinstance(x, ast.Call):
            f = x.func
            called.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    method_nodes = {id(m) for ms in a.methods.values() for m in ms}
    rec_kind = None
    note = None
    space_label = None
    pool = []
    for fn in list(a.funcs.values()):
        if id(fn) in method_nodes:
            continue
        if fn.name in called and not (t == C1 and s == C1):
            info = a.info(fn)
            if info and info["recursive"] and info["time"] >= t:
                pool.append(info)
            continue
        info = a.info(fn)
        if info:
            pool.append(info)
    for info in pool:
        if info["time"] > t or (info["time"] == t and info["space"] >= s):
            t = _max(t, info["time"])
            s = _max(s, info["space"])
            note = info.get("note") or note
            rec_kind = info.get("kind") or rec_kind
            space_label = info.get("space_label") or space_label
        elif info["recursive"] and info["time"] == t:
            note = info.get("note") or note
    # class based programs: report cost per operation
    per_method = None
    if a.classes:
        per_method = []
        space_grows = False
        for cname, ms in a.methods.items():
            for m in ms:
                if m.name.startswith("__") and m.name != "__init__":
                    continue
                if m.name == "__init__":
                    continue
                info = a.info(m)
                if info:
                    per_method.append((m.name, info["time"]))
                    mctx = info["ctx"]
                    for x in ast.walk(m):
                        if isinstance(x, ast.Call):
                            nm = getattr(x.func, "attr", getattr(x.func, "id", ""))
                            if nm in _GROW or nm in a.classes:
                                space_grows = True
        if per_method:
            seen = set()
            uniq = [(n, c) for n, c in per_method if not (n in seen or seen.add(n))]
            time_str = ", ".join("%s: %s" % (n, fmt(c)) for n, c in uniq[:5])
            if len(uniq) > 5:
                time_str += ", …"
            worst = max(c for _, c in uniq)
            t = worst
            space_str = "O(n) – stores the elements" if space_grows else "O(1)"
            if not space_grows:
                s = C1
            else:
                s = CN
    time_str_final = fmt(t)
    space_str_final = space_label or fmt(s)
    label, cat = _label(a, tree, t, a.funcs, a.classes, rec_kind)
    if per_method:
        time_str_final = time_str + "  (per operation)"
        space_str_final = space_str
        note = None
    f = a.features
    worst_s = fmt(t)
    if f["early_exit"] and not f["recursion"] and t >= CN and label not in ("Bubble Sort",):
        best = "O(1)  – can stop at the first match"
    elif label in ("Bubble Sort", "Insertion Sort") and f["early_exit"]:
        best = "O(n)  – already sorted input"
    elif f["recursion"] == "halve" and t == CLOG:
        best = "O(1)  – found at the first check"
    else:
        best = worst_s + "  – same for every input"
    if label == "Quick Sort":
        t_str = "Average O(n log n), worst O(n²)"
        time_str_final = t_str
        worst_s = "O(n²)"
        best = "O(n log n)"
    if f["graph"] and not per_method:
        time_str_final = "O((V + E) log V)" if (t[2] and a.features["graph"] and "Dijkstra" in label) or t == CNLOGN else "O(V + E)"
        space_str_final = "O(V)"
        worst_s = time_str_final
        best = time_str_final + "  – every reachable vertex/edge is visited"
    result = {
        "algorithm": label,
        "category": cat,
        "time_complexity": time_str_final,
        "space_complexity": space_str_final,
        "best_case": best,
        "worst_case": worst_s,
        "key_operations": _explain(a, t, s, rec_kind, note),
        "explanation": _explain(a, t, s, rec_kind, note),
        "confidence": "high",
        "note": "Estimated from the code's structure. n = size of the input; extra memory only (input not counted).",
    }
    return result
