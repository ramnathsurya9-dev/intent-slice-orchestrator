"""Linux traffic control: one shared HTB tree on the Docker bridge, container -> veth discovery.

Tree (all rates in Mbit/s):

    1:   root htb, unmatched traffic -> public class
    1:1    whole link (e.g. 100)
      1:2    commercial class, hard cap (e.g. 80)      <- eMBB and URLLC share this
        1:10 eMBB   rate=guarantee ceil=ceiling prio 1
        1:20 URLLC  rate=guarantee ceil=ceiling prio 0  (served first when borrowing)
      1:30   public rate=reserve    ceil=reserve or link, prio 2

A slice may borrow idle bandwidth up to its ceiling, and gets its guarantee back when its owner
needs it. Commercial slices can never exceed the commercial class, so the public reserve is
never taken. Packets are matched by destination IP and shaped on the bridge's egress, i.e.
traffic from the host (or routed through it) toward the containers.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
COMMERCIAL_MINOR = 2


class TcError(RuntimeError):
    pass


class Runner(Protocol):
    def run(self, argv: Sequence[str], check: bool = True) -> str: ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], check: bool = True) -> str:
        try:
            proc = subprocess.run(list(argv), check=False, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TcError(f"{' '.join(argv)}: {exc}") from exc
        if check and proc.returncode != 0:
            raise TcError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
        return proc.stdout


class DryRunRunner:
    """Records commands instead of running them."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, argv: Sequence[str], check: bool = True) -> str:
        self.commands.append(list(argv))
        del self.commands[:-500]
        return ""


@dataclass(frozen=True)
class ClassSpec:
    minor: int  # HTB class 1:<minor>
    ip: str
    rate_mbps: int  # guaranteed
    ceil_mbps: int  # may borrow up to this
    prio: int  # HTB prio: lower number is served first
    latency_ms: int
    commercial: bool


_STATS_SPLIT_RE = re.compile(r"(?m)^class htb 1:(\d+) ")
_SENT_RE = re.compile(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) requeues (\d+)\)")
_BACKLOG_RE = re.compile(r"backlog (\d+)b (\d+)p")


def parse_class_stats(text: str) -> dict[int, dict[str, int]]:
    """Per-class counters from `tc -s class show dev <iface>`, keyed by class minor number."""
    out: dict[int, dict[str, int]] = {}
    parts = _STATS_SPLIT_RE.split(text)  # ["", "1", block, "2", block, ...]
    for minor, block in zip(parts[1::2], parts[2::2], strict=False):
        sent = _SENT_RE.search(block)
        if not sent:
            continue
        backlog = _BACKLOG_RE.search(block)
        out[int(minor)] = {
            "sent_bytes": int(sent.group(1)),
            "sent_pkts": int(sent.group(2)),
            "dropped": int(sent.group(3)),
            "overlimits": int(sent.group(4)),
            "backlog_bytes": int(backlog.group(1)) if backlog else 0,
        }
    return out


def _docker_exec(container: str, argv: list[str]) -> str:
    import docker

    result = docker.from_env().containers.get(container).exec_run(argv)
    if result.exit_code != 0:
        raise TcError(f"{' '.join(argv)} in {container} failed: {result.output.decode().strip()}")
    return result.output.decode()


class TrafficManager:
    def __init__(
        self,
        runner: Runner,
        *,
        sys_class_net: str = "/sys/class/net",
        container_exec: Callable[[str, list[str]], str] = _docker_exec,
        interface_resolver: Callable[[str], str] | None = None,
        verify_iface: bool = True,
    ) -> None:
        self.runner = runner
        self.sys_net = Path(sys_class_net)
        self._exec = container_exec
        self._resolver = interface_resolver
        self._verify = verify_iface

    # ----- discovery -----
    def discover_interface(self, container: str) -> str:
        """Find the host-side veth of a container (used for display and sanity checks).

        The container's eth0 reports the host interface index of its peer in `iflink`;
        the host interface with that `ifindex` is the veth.
        """
        if self._resolver:
            return self._resolver(container)
        out = self._exec(container, ["cat", "/sys/class/net/eth0/iflink"]).strip()
        try:
            peer_index = int(out)
        except ValueError as exc:
            raise TcError(f"unexpected iflink from {container}: {out!r}") from exc
        for entry in sorted(self.sys_net.iterdir()):
            try:
                if int((entry / "ifindex").read_text()) == peer_index:
                    return entry.name
            except (OSError, ValueError):
                continue
        raise TcError(f"no host interface with index {peer_index} for {container}")

    # ----- validation -----
    @staticmethod
    def _check_iface(interface: str) -> None:
        if not IFACE_RE.fullmatch(interface):
            raise ValueError(f"invalid interface name: {interface!r}")

    @staticmethod
    def _check_spec(spec: ClassSpec) -> None:
        ipaddress.IPv4Address(spec.ip)
        if not 1 <= spec.rate_mbps <= 100_000:
            raise ValueError("rate_mbps must be 1..100000")
        if not spec.rate_mbps <= spec.ceil_mbps <= 100_000:
            raise ValueError("ceil_mbps must be between rate_mbps and 100000")
        if not 0 <= spec.latency_ms <= 10_000:
            raise ValueError("latency_ms must be 0..10000")
        if not 0 <= spec.prio <= 7:
            raise ValueError("prio must be 0..7")

    # ----- commands -----
    def clear(self, interface: str) -> None:
        """Remove our qdisc tree. Silent when there is nothing to remove."""
        self._check_iface(interface)
        self.runner.run(["tc", "qdisc", "del", "dev", interface, "root"], check=False)

    def _class_cmd(self, verb: str, iface: str, spec: ClassSpec) -> list[str]:
        parent = f"1:{COMMERCIAL_MINOR}" if spec.commercial else "1:1"
        return [
            "tc", "class", verb, "dev", iface, "parent", parent, "classid", f"1:{spec.minor}",
            "htb", "rate", f"{spec.rate_mbps}mbit", "ceil", f"{spec.ceil_mbps}mbit",
            "prio", str(spec.prio),
        ]

    def _leaf(self, iface: str, spec: ClassSpec) -> None:
        """Attach (or update) the leaf queue: netem when extra delay is wanted, else a plain FIFO."""
        base = ["tc", "qdisc", "replace", "dev", iface, "parent", f"1:{spec.minor}", "handle", f"{spec.minor}:"]
        if spec.latency_ms > 0:
            self.runner.run([*base, "netem", "delay", f"{spec.latency_ms}ms"])
        else:
            self.runner.run([*base, "pfifo"])

    def setup_tree(
        self, iface: str, link_mbps: int, commercial_mbps: int, specs: Sequence[ClassSpec]
    ) -> None:
        """Build the whole tree from scratch. Called once at startup."""
        self._check_iface(iface)
        if self._verify and not (self.sys_net / iface).exists():
            raise TcError(f"interface {iface} not found (is the Docker network bridge named {iface}?)")
        if not 0 < commercial_mbps <= link_mbps:
            raise ValueError("commercial_mbps must be 1..link_mbps")
        for spec in specs:
            self._check_spec(spec)
        public = [s for s in specs if not s.commercial]
        if len(public) != 1:
            raise ValueError("exactly one non-commercial (public) class is required")

        self.clear(iface)
        run = self.runner.run
        run(["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", str(public[0].minor)])
        run(["tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:1",
             "htb", "rate", f"{link_mbps}mbit", "ceil", f"{link_mbps}mbit"])
        run(["tc", "class", "add", "dev", iface, "parent", "1:1", "classid", f"1:{COMMERCIAL_MINOR}",
             "htb", "rate", f"{commercial_mbps}mbit", "ceil", f"{commercial_mbps}mbit"])
        for spec in specs:
            run(self._class_cmd("add", iface, spec))
            self._leaf(iface, spec)
            run(["tc", "filter", "add", "dev", iface, "parent", "1:", "protocol", "ip", "prio", "1",
                 "u32", "match", "ip", "dst", f"{spec.ip}/32", "flowid", f"1:{spec.minor}"])

    def update_class(self, iface: str, spec: ClassSpec) -> None:
        """Change one slice's guarantee, ceiling, priority or delay without touching the others."""
        self._check_iface(iface)
        self._check_spec(spec)
        self.runner.run(self._class_cmd("replace", iface, spec))
        self._leaf(iface, spec)

    # ----- telemetry -----
    def class_stats(self, iface: str) -> dict[int, dict[str, int]]:
        self._check_iface(iface)
        return parse_class_stats(self.runner.run(["tc", "-s", "class", "show", "dev", iface], check=False))
