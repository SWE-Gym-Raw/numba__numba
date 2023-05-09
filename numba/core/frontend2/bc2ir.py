from dataclasses import dataclass, replace, field, fields
import dis
from contextlib import contextmanager
from typing import Optional, Iterator, Protocol, runtime_checkable, Union
from collections import (
    deque,
    defaultdict,
)
from collections.abc import Mapping

from . import bcinterp

from numba_rvsdg.core.datastructures.byte_flow import ByteFlow
from numba_rvsdg.core.datastructures.scfg import (
    SCFG,
    ConcealedRegionView,
)
from numba_rvsdg.core.datastructures.basic_block import (
    BasicBlock,
    PythonBytecodeBlock,
    RegionBlock,
    BranchBlock,
    ControlVariableBlock,
)
from numba_rvsdg.core.datastructures.labels import Label
from numba_rvsdg.rendering.rendering import ByteFlowRenderer

from .renderer import RvsdgRenderer

from numba.core.utils import MutableSortedSet, MutableSortedMap


@dataclass(frozen=True)
class ValueState:
    parent: Optional["Op"]
    name: str
    out_index: int
    is_effect: bool = False

    def short_identity(self) -> str:
        return f"ValueState({id(self.parent):x}, {self.name}, {self.out_index})"


@dataclass(frozen=True)
class Op:
    opname: str
    bc_inst: Optional[dis.Instruction]
    _inputs: dict[str, ValueState] = field(default_factory=dict)
    _outputs: dict[str, ValueState] = field(default_factory=dict)

    def add_input(self, name, vs: ValueState):
        self._inputs[name] = vs

    def add_output(self, name: str, is_effect=False) -> ValueState:
        vs = ValueState(parent=self, name=name, out_index=len(self._outputs), is_effect=is_effect)
        self._outputs[name] = vs
        return vs

    def short_identity(self) -> str:
        return f"Op({self.opname}, {id(self):x})"

    def summary(self) -> str:
        ins = ', '.join([k for k in self._inputs])
        outs = ', '.join([k for k in self._outputs])
        bc = "---"
        if self.bc_inst is not None:
            bc = f"{self.bc_inst.opname}({self.bc_inst.argrepr})"
        return f"Op\n{self.opname}\n{bc}\n({ins}) -> ({outs}) "

    @property
    def outputs(self):
        return list(self._outputs.values())


@runtime_checkable
class DDGProtocol(Protocol):
    incoming_states: MutableSortedSet[str]
    outgoing_states: MutableSortedSet[str]


@dataclass(frozen=True)
class DDGRegion(RegionBlock):
    incoming_states: MutableSortedSet[str] = field(default_factory=MutableSortedSet)
    outgoing_states: MutableSortedSet[str] = field(default_factory=MutableSortedSet)

    @contextmanager
    def render_rvsdg(self, renderer, digraph, label):
        with digraph.subgraph(name=f"cluster_rvsdg_{id(self)}") as subg:
            subg.attr(color="black", label="region", bgcolor="grey")
            subg.node(f"incoming_{id(self)}", label=f"{'|'.join([f'<{k}> {k}' for k in self.incoming_states])}", shape='record', rank="min")
            subg.edge(f"incoming_{id(self)}", f"cluster_{label}", style="invis")
            yield subg
            subg.edge(f"cluster_{label}", f"outgoing_{id(self)}", style="invis")
            subg.node(f"outgoing_{id(self)}", label=f"{'|'.join([f'<{k}> {k}' for k in self.outgoing_states])}", shape='record', rank="max")

@dataclass(frozen=True)
class DDGBranch(BranchBlock):
    incoming_states: MutableSortedSet[str] = field(default_factory=MutableSortedSet)
    outgoing_states: MutableSortedSet[str] = field(default_factory=MutableSortedSet)

@dataclass(frozen=True)
class DDGControlVariable(ControlVariableBlock):
    incoming_states: MutableSortedSet[str] = field(default_factory=MutableSortedSet)
    outgoing_states: MutableSortedSet[str] = field(default_factory=MutableSortedSet)


@dataclass(frozen=True)
class DDGBlock(BasicBlock):
    in_effect: ValueState | None = None
    out_effect: ValueState | None = None
    in_stackvars: list[ValueState] = field(default_factory=list)
    out_stackvars: list[ValueState] = field(default_factory=list)
    in_vars: MutableSortedMap[str, ValueState] = field(default_factory=MutableSortedMap)
    out_vars: MutableSortedMap[str, ValueState] = field(default_factory=MutableSortedMap)

    def __post_init__(self):
        assert isinstance(self.in_vars, MutableSortedMap)
        assert isinstance(self.out_vars, MutableSortedMap)

    def render_rvsdg(self, renderer, digraph, label):
        with digraph.subgraph(name="cluster_"+str(label)) as g:
            g.attr(color='lightgrey')
            g.attr(label=str(label))
            # render body
            self.render_valuestate(renderer, g, self.in_effect)
            self.render_valuestate(renderer, g, self.out_effect)
            for vs in self.in_stackvars:
                self.render_valuestate(renderer, g, vs)
            for vs in self.out_stackvars:
                self.render_valuestate(renderer, g, vs)
            for vs in self.out_vars.values():
                self.render_valuestate(renderer, g, vs)
            # Fill incoming
            in_vars_fields = "incoming-vars|" + "|".join([f"<{x}> {x}" for x in self.in_vars])
            fields = "|" + in_vars_fields
            g.node(f"incoming_{id(self)}", shape="record", label=f"{fields}", rank="source")

            for vs in self.in_vars.values():
                self.add_vs_edge(renderer, f"incoming_{id(self)}:{vs.name}", vs.parent.short_identity())

            # Fill outgoing
            out_stackvars_fields = "outgoing-stack|" + "|".join([f"<{x.name}> {x.name}" for x in self.out_stackvars])
            out_vars_fields = "outgoing-vars|" + "|".join([f"<{x.name}> {x.name}" for x in self.out_vars.values()])
            fields = f"<{self.out_effect.short_identity()}> env" + "|" + out_stackvars_fields + "|" + out_vars_fields
            g.node(f"outgoing_{id(self)}", shape="record", label=f"{fields}")
            for vs in self.out_stackvars:
                self.add_vs_edge(renderer, vs, f"outgoing_{id(self)}:{vs.name}")
            for vs in self.out_vars.values():
                self.add_vs_edge(renderer, vs, f"outgoing_{id(self)}:{vs.name}")
            self.add_vs_edge(renderer, self.out_effect, f"outgoing_{id(self)}:{self.out_effect.short_identity()}")
            # Draw "head"
            g.node(str(label), shape="doublecircle", label="")

    def render_valuestate(self, renderer, digraph, vs: ValueState, *, follow=True):
        if vs.is_effect:
            digraph.node(vs.short_identity(), shape="circle", label=str(vs.name))
        else:
            digraph.node(vs.short_identity(), shape="rect", label=str(vs.name))
        if follow and vs.parent is not None:
            op = vs.parent
            self.render_op(renderer, digraph, op)

    def render_op(self, renderer, digraph, op: Op):
        op_anchor = op.short_identity()
        digraph.node(op_anchor, label=op.summary(),
                     shape="box", style="rounded")
        for edgename, vs in op._outputs.items():
            self.add_vs_edge(renderer, op_anchor, vs, taillabel=f"{edgename}")
            self.render_valuestate(renderer, digraph, vs, follow=False)
        for edgename, vs in op._inputs.items():
            self.add_vs_edge(renderer, vs, op_anchor, headlabel=f"{edgename}")
            self.render_valuestate(renderer, digraph, vs)

    def add_vs_edge(self, renderer, src, dst, **attrs):
        is_effect = (isinstance(src, ValueState) and src.is_effect) or (isinstance(dst, ValueState) and dst.is_effect)
        if isinstance(src, ValueState):
            src = src.short_identity()
        if isinstance(dst, ValueState):
            dst = dst.short_identity()

        kwargs = attrs
        if is_effect:
            kwargs["style"] = "dotted"

        renderer.add_edge(src, dst, **kwargs)

    @property
    def incoming_states(self) -> MutableSortedSet:
        return MutableSortedSet(self.in_vars)

    @property
    def outgoing_states(self) -> MutableSortedSet:
        return MutableSortedSet(self.out_vars)


def render_scfg(byteflow):
    bfr = ByteFlowRenderer()
    bfr.bcmap_from_bytecode(byteflow.bc)
    bfr.render_scfg(byteflow.scfg).view("scfg")


def canonicalize_scfg(scfg: SCFG):
    todos = set(scfg.graph)
    while todos:
        label = todos.pop()
        blk = scfg[label]
        todos.discard(label)
        if isinstance(blk, RegionBlock):
            if blk.kind == 'head':
                # Make sure that branches are in switch blocks
                branches = blk.jump_targets
                branch_targets = set()
                for br in branches:
                    branch_targets |= set(scfg[br].jump_targets)
                [tail] = branch_targets
                tailblk = scfg[tail]
                switch_labels = {label, tail, *branches}
                subregion_graph = {k:scfg[k] for k in switch_labels}
                scfg.remove_blocks(switch_labels)
                scfg.graph[label] = RegionBlock(
                    label=label,
                    kind="switch",
                    _jump_targets=tailblk._jump_targets,
                    backedges=tailblk.backedges,
                    exiting=tailblk.exiting,
                    headers={label},
                    subregion=SCFG(graph=subregion_graph, clg=scfg.clg),
                )
                todos -= switch_labels
            elif blk.kind == 'loop':
                canonicalize_scfg(blk.subregion)
                if blk.exiting not in blk.subregion:
                    [exiting], _exit = blk.subregion.find_exiting_and_exits(set(blk.subregion.graph))
                    scfg.graph[label] = replace(blk, exiting=exiting)


def build_rvsdg(code):
    byteflow = ByteFlow.from_bytecode(code)
    byteflow = byteflow.restructure()
    canonicalize_scfg(byteflow.scfg)
    # render_scfg(byteflow)
    rvsdg = convert_to_dataflow(byteflow)
    rvsdg = propagate_states(rvsdg)
    RvsdgRenderer().render_rvsdg(rvsdg).view("rvsdg")


def _compute_incoming_labels(graph: Mapping[Label, BasicBlock]) -> dict[Label, set[Label]]:
    jump_table: dict[Label, set[Label]] = {}
    blk: BasicBlock
    for k in graph:
        jump_table[k] = set()
    for blk in graph.values():
        for dst in blk.jump_targets:
            if dst in jump_table:
                jump_table[dst].add(blk.label)
    return jump_table

def _flatten_full_graph(scfg: SCFG):
    from collections import ChainMap
    regions = [_flatten_full_graph(elem.subregion)
               for elem in scfg.graph.values()
               if isinstance(elem, RegionBlock)]
    out = ChainMap(*regions, scfg.graph)
    for blk in out.values():
        assert not isinstance(blk, RegionBlock), type(blk)
    return out


DDGTypes = (DDGBlock, DDGControlVariable, DDGBranch)
_DDGTypeAnn = Union[DDGBlock, DDGControlVariable, DDGBranch]

def view_toposorted_ddgblock_only(rvsdg: SCFG) -> list[list[_DDGTypeAnn]]:
    """Return toposorted nested list of DDGTypes
    """
    graph = _flatten_full_graph(rvsdg)
    incoming_labels = _compute_incoming_labels(graph)
    visited: set[Label] = set()
    toposorted: list[list[Label]] = []

    # Toposort
    while incoming_labels:
        level = []
        for k, vs in incoming_labels.items():
            if not (vs - visited):
                # all incoming visited
                level.append(k)
        for k in level:
            del incoming_labels[k]
        visited |= set(level)
        toposorted.append(level)

    # Filter
    output: list[list[_DDGTypeAnn]] = []
    for level in toposorted:
        filtered = [graph[k] for k in level if isinstance(graph[k], DDGTypes)]
        if filtered:
            output.append(filtered)

    return output


def convert_to_dataflow(byteflow: ByteFlow) -> SCFG:
    bcmap = {inst.offset: inst for inst in byteflow.bc}
    rvsdg = convert_scfg_to_dataflow(byteflow.scfg, bcmap)
    return rvsdg

def propagate_states(rvsdg: SCFG) -> SCFG:
    # vars
    propagate_states_ddgblock_only_inplace(rvsdg)
    propagate_states_to_parent_region_inplace(rvsdg)
    propagate_states_to_outgoing_inplace(rvsdg)

    # stack
    propagate_stack(rvsdg)
    return rvsdg

def propagate_states_ddgblock_only_inplace(rvsdg: SCFG):
    # Propagate the outgoing states
    topo_ddgblocks = view_toposorted_ddgblock_only(rvsdg)
    block_vars: dict[Label, set[str]] = {}
    live_vars: set[str] = set()
    for blklevel in topo_ddgblocks:
        new_vars: set[str] = set()
        for blk in blklevel:
            block_vars[blk.label] = live_vars.copy()
            new_vars |= set(blk.outgoing_states)
        live_vars |= new_vars

    # Apply changes
    for blklevel in topo_ddgblocks:
        for blk in blklevel:
            extra_vars = block_vars[blk.label] - set(blk.incoming_states)
            for k in extra_vars:
                if isinstance(blk, DDGBlock):
                    op = Op(opname="var.incoming", bc_inst=None)
                    vs = op.add_output(k)
                    blk.in_vars[k] = vs
                    blk.out_vars[k] = vs
                else:
                    blk.incoming_states.add(k)
                    blk.outgoing_states.add(k)


def _walk_all_regions(scfg: SCFG) -> Iterator[RegionBlock]:
    for blk in scfg.graph.values():
        if isinstance(blk, RegionBlock):
            yield from _walk_all_regions(blk.subregion)
            yield blk


def propagate_states_to_parent_region_inplace(rvsdg: SCFG):
    for reg in _walk_all_regions(rvsdg):
        assert isinstance(reg, DDGRegion)
        subregion: SCFG = reg.subregion
        head = subregion[subregion.find_head()]
        exit = subregion[reg.exiting]
        if isinstance(head, DDGProtocol):
            reg.incoming_states.update(head.incoming_states)
        if isinstance(exit, DDGProtocol):
            reg.outgoing_states.update(reg.incoming_states)
            reg.outgoing_states.update(exit.outgoing_states)


def propagate_states_to_outgoing_inplace(rvsdg: SCFG):
    for src in rvsdg.graph.values():
        for dst_label in src.jump_targets:
            if dst_label in rvsdg.graph:
                dst = rvsdg.graph[dst_label]
                if isinstance(dst, DDGRegion):
                    dst.incoming_states.update(src.outgoing_states)
                    dst.outgoing_states.update(dst.incoming_states)
                    propagate_states_to_outgoing_inplace(dst.subregion)



# class RegionVisitor:
#     def visit_linear(self, region: RegionBlock):
#         pass

#     def visit_loop(self, region: RegionBlock):
#         pass

#     def visit_branch(self, head: Label: RegionBlock):
#         pass

#     # def visit(self, scfg: SCFG):
    #     for each in

def propagate_stack(rvsdg: SCFG):
    pass


def _upgrade_dataclass(old, newcls, replacements=None):
    if replacements is None:
        replacements = {}
    fieldnames = [fd.name for fd in fields(old)]
    oldattrs = {k: getattr(old, k) for k in fieldnames
                if k not in replacements}
    return newcls(**oldattrs, **replacements)


def convert_scfg_to_dataflow(scfg, bcmap) -> SCFG:
    rvsdg = SCFG()
    for block in scfg.graph.values():
        # convert block
        if isinstance(block, PythonBytecodeBlock):
            ddg = convert_bc_to_ddg(block, bcmap)
            rvsdg.add_block(ddg)
        elif isinstance(block, RegionBlock):
            # Inside-out
            subregion = convert_scfg_to_dataflow(block.subregion, bcmap)
            rvsdg.add_block(_upgrade_dataclass(block, DDGRegion,
                                               dict(subregion=subregion)))
        elif isinstance(block, BranchBlock):
            rvsdg.add_block(_upgrade_dataclass(block, DDGBranch))
        elif isinstance(block, ControlVariableBlock):
            rvsdg.add_block(_upgrade_dataclass(block, DDGControlVariable))
        else:
            raise Exception("unreachable")

    return rvsdg


def convert_bc_to_ddg(block: PythonBytecodeBlock, bcmap: dict[int, dis.Bytecode]):
    instlist = block.get_instructions(bcmap)
    converter = BC2DDG()
    in_effect = converter.effect
    for inst in instlist:
        converter.convert(inst)
    blk = DDGBlock(
        label=block.label,
        _jump_targets=block._jump_targets,
        backedges=block.backedges,
        in_effect=in_effect,
        out_effect=converter.effect,
        in_stackvars=list(converter.incoming_stackvars),
        out_stackvars=list(converter.stack),
        in_vars=MutableSortedMap(converter.incoming_vars),
        out_vars=MutableSortedMap(converter.varmap),
    )

    return blk

class BC2DDG:
    def __init__(self):
        self.stack: list[ValueState] = []
        start_env = Op("start", bc_inst=None)
        self.effect = start_env.add_output("env", is_effect=True)
        self.varmap: dict[str, ValueState] = {}
        self.incoming_vars: dict[str, ValueState] = {}
        self.incoming_stackvars: list[ValueState] = []

    def push(self, val: ValueState):
        self.stack.append(val)

    def pop(self) -> ValueState:
        if not self.stack:
            op = Op(opname="stack.incoming", bc_inst=None)
            vs = op.add_output(f"stack.{len(self.incoming_stackvars)}")
            self.stack.append(vs)
            self.incoming_stackvars.append(vs)
        return self.stack.pop()

    def top(self) -> ValueState:
        tos = self.pop()
        self.push(tos)
        return tos

    def _decorate_varname(self, varname: str) -> str:
        return f"var.{varname}"

    def store(self, varname: str, value: ValueState):
        self.varmap[varname] = value

    def load(self, varname: str) -> ValueState:
        if varname not in self.varmap:
            op = Op(opname="var.incoming", bc_inst=None)
            vs = op.add_output(varname)
            self.incoming_vars[varname] = vs
            self.varmap[varname] = vs

        return self.varmap[varname]

    def replace_effect(self, env: ValueState):
        assert env.is_effect
        self.effect = env

    def convert(self, inst: dis.Instruction):
        fn = getattr(self, f"op_{inst.opname}")
        fn(inst)

    def op_RESUME(self, inst: dis.Instruction):
        pass   # no-op

    def op_LOAD_GLOBAL(self, inst: dis.Instruction):
        load_nil = inst.arg & 1
        op = Op(opname="global", bc_inst=inst)
        op.add_input("env", self.effect)
        nil = op.add_output("nil")
        if load_nil:
            self.push(nil)
        self.push(op.add_output(f"{inst.argval}"))

    def op_LOAD_CONST(self, inst: dis.Instruction):
        op = Op(opname="const", bc_inst=inst)
        self.push(op.add_output("out"))

    def op_STORE_FAST(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="store", bc_inst=inst)
        op.add_input("value", tos)
        varname = self._decorate_varname(inst.argval)
        self.store(varname, op.add_output(varname))

    def op_LOAD_FAST(self, inst: dis.Instruction):
        varname = self._decorate_varname(inst.argval)
        self.push(self.load(varname))

    def op_PRECALL(self, inst: dis.Instruction):
        pass # no-op

    def op_CALL(self, inst: dis.Instruction):
        argc: int = inst.argval
        callable = self.pop()  # TODO
        arg0 = self.pop() # TODO
        # TODO: handle kwnames
        args = reversed([arg0, *[self.pop() for _ in range(argc)]])
        op = Op(opname="call", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("callee", callable)
        for i, arg in enumerate(args):
            op.add_input(f"arg.{i}", arg)
        self.replace_effect(op.add_output("env", is_effect=True))
        self.push(op.add_output("ret"))

    def op_GET_ITER(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="getiter", bc_inst=inst)
        op.add_input("obj", tos)
        self.push(op.add_output("iter"))

    def op_FOR_ITER(self, inst: dis.Instruction):
        tos = self.top()
        op = Op(opname="foriter", bc_inst=inst)
        op.add_input("iter", tos)
        self.push(op.add_output("indvar"))

    def op_BINARY_OP(self, inst: dis.Instruction):
        rhs = self.pop()
        lhs = self.pop()
        op = Op(opname="binaryop", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("lhs", lhs)
        op.add_input("rhs", rhs)
        self.replace_effect(op.add_output("env", is_effect=True))
        self.push(op.add_output("out"))

    def op_RETURN_VALUE(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="ret", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("retval", tos)
        self.replace_effect(op.add_output("env", is_effect=True))

    def op_JUMP_FORWARD(self, inst: dis.Instruction):
        pass # no-op

    def op_JUMP_BACKWARD(self, inst: dis.Instruction):
        pass # no-op

    def op_POP_JUMP_FORWARD_IF_FALSE(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op("jump.if_false", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("pred", tos)
        self.replace_effect(op.add_output("env", is_effect=True))


def run_frontend(func): #, inline_closures=False, emit_dels=False):
    # func_id = bytecode.FunctionIdentity.from_function(func)

    rvsdg = build_rvsdg(func.__code__)

    return rvsdg
    # bc = bytecode.ByteCode(func_id=func_id)
    # interp = bcinterp.Interpreter(func_id)
    # func_ir = interp.interpret(bc)
    # return func_ir
