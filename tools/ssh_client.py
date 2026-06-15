"""ww/tools/ssh_client.py — WW SSH remoteexecuteclient

Allows WW to SSH to a remote host to execute commands.
Supports key authentication and password authentication (via sshpass).
"""

from __future__ import annotations
import json
import os
import subprocess
from typing import Any, Dict, List, Optional


class SSHClient:
    """
    SSH remoteexecuteclient. 
    
    usesystem ssh command (supports ~/.ssh/config) . 
    """
    
    def __init__(self, host: str, user: str = "",
                 port: int = 22, identity_file: str = "",
                 password: str = "", use_sshpass: bool = False):
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.password = password
        self.use_sshpass = use_sshpass or bool(password)
    
    def _ssh_command(self, remote_cmd: str) -> List[str]:
        """Build SSH command."""
        if self.use_sshpass and self.password:
            cmd = ["sshpass", "-p", self.password, "ssh"]
        else:
            cmd = ["ssh"]
        
        # SSH option
        if self.port != 22:
            cmd += ["-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", os.path.expanduser(self.identity_file)]
        
        # StrictHostKeyChecking=no only for known hosts
        cmd += ["-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes" if not self.use_sshpass else "BatchMode=no"]
        
        # host
        target = self.host
        if self.user:
            target = self.user + "@" + self.host
        cmd.append(target)
        cmd.append(remote_cmd)
        
        return cmd
    
    def run(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """at remotehostexecutecommand. """
        cmd = self._ssh_command(command)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "success": result.returncode == 0,
                "output": (result.stdout or "").strip()[:3000],
                "error": (result.stderr or "").strip()[:500],
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"SSH timeout ({timeout}s)"}
        except FileNotFoundError:
            return {"success": False, "error": "sshpass not installed (apt install sshpass)" if self.use_sshpass else "ssh not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def copy_to(self, local_path: str, remote_path: str) -> Dict:
        """copyfileto remotehost (via  scp) . """
        target = self.host
        if self.user:
            target = self.user + "@" + self.host
        
        try:
            scp_cmd = ["scp"]
            if self.port != 22:
                scp_cmd += ["-P", str(self.port)]
            if self.identity_file:
                scp_cmd += ["-i", os.path.expanduser(self.identity_file)]
            scp_cmd += ["-o", "StrictHostKeyChecking=accept-new"]
            
            if self.use_sshpass and self.password:
                scp_cmd = ["sshpass", "-p", self.password] + scp_cmd
            
            scp_cmd += [os.path.expanduser(local_path), f"{target}:{remote_path}"]
            
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
            return {"success": result.returncode == 0, "output": (result.stdout or result.stderr)[:500]}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def copy_from(self, remote_path: str, local_path: str) -> Dict:
        """from remotehostcopyfile. """
        target = self.host
        if self.user:
            target = self.user + "@" + self.host
        
        try:
            scp_cmd = ["scp"]
            if self.port != 22:
                scp_cmd += ["-P", str(self.port)]
            if self.identity_file:
                scp_cmd += ["-i", os.path.expanduser(self.identity_file)]
            scp_cmd += ["-o", "StrictHostKeyChecking=accept-new"]
            
            if self.use_sshpass and self.password:
                scp_cmd = ["sshpass", "-p", self.password] + scp_cmd
            
            scp_cmd += [f"{target}:{remote_path}", os.path.expanduser(local_path)]
            
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
            return {"success": result.returncode == 0, "output": (result.stdout or result.stderr)[:500]}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def test_connection(self) -> Dict:
        """test SSH connection. """
        return self.run("echo 'SSH OK' && hostname && uptime -p", timeout=15)
    
    def close(self):
        """Clean up."""
        pass


# ── SSH connection pool ──

class SSHPool:
    """
    SSH connection pool.
    
    Manage multiple remote host SSH clients.
    Auto reuse connection configuration.
    """
    
    def __init__(self):
        self._clients: Dict[str, SSHClient] = {}
        self._defaults: Dict = {}
    
    def register(self, name: str, host: str, user: str = "",
                 port: int = 22, identity_file: str = "",
                 password: str = ""):
        """registera  SSH host. """
        client = SSHClient(
            host=host,
            user=user,
            port=port,
            identity_file=identity_file,
            password=password,
            use_sshpass=bool(password),
        )
        self._clients[name] = client
        return client
    
    def get(self, name: str) -> Optional[SSHClient]:
        """get SSH client. """
        return self._clients.get(name)
    
    def run(self, name: str, command: str, timeout: int = 30) -> Dict:
        """Execute command on specified host."""
        client = self.get(name)
        if not client:
            return {"success": False, "error": f"unknown host: {name}"}
        return client.run(command, timeout)
    
    def list(self) -> List[Dict]:
        """List all hosts."""
        return [{"name": name, "host": c.host, "user": c.user}
                for name, c in self._clients.items()]
    
    def remove(self, name: str):
        """Remove a host."""
        if name in self._clients:
            del self._clients[name]


# default SSH connectionsetting
from tools.registry import ToolDef

_ssh_pool_instance = None

def get_ssh_pool() -> SSHPool:
    """Get global SSH Pool (singleton)."""
    global _ssh_pool_instance
    if _ssh_pool_instance is None:
        _ssh_pool_instance = SSHPool()
        # from environment variabledynamicregisterhost
        # format: WW_SSH_HOSTS='{"node1":{"host":"x.x","user":"u","password":"p"}}'
        hosts_json = os.environ.get("WW_SSH_HOSTS", "{}")
        try:
            hosts = json.loads(hosts_json)
            for name, cfg in hosts.items():
                _ssh_pool_instance.register(
                    name,
                    host=cfg.get("host", "localhost"),
                    user=cfg.get("user", "root"),
                    password=cfg.get("password", ""),
                    port=cfg.get("port", 22),
                )
        except (json.JSONDecodeError, TypeError):
            pass
    return _ssh_pool_instance


# ── toolprocessor ──

def _ssh_run_handler(host: str, command: str, timeout: int = 30) -> Dict:
    """at remotehostexecute SSH command. """
    pool = get_ssh_pool()
    return pool.run(host, command, timeout)


def _ssh_copy_handler(host: str, source: str, destination: str,
                      direction: str = "to") -> Dict:
    """SSH file transfer. direction: 'to' (local→remote) or 'from' (remote→local)."""
    client = get_ssh_pool().get(host)
    if not client:
        return {"success": False, "error": f"unknown host: {host}"}
    if direction == "to":
        return client.copy_to(source, destination)
    return client.copy_from(source, destination)


def _ssh_test_handler(host: str) -> Dict:
    """test SSH connection. """
    client = get_ssh_pool().get(host)
    if not client:
        return {"success": False, "error": f"unknown host: {host}"}
    return client.test_connection()


def _ssh_hosts_handler() -> Dict:
    """List registered SSH hosts."""
    pool = get_ssh_pool()
    return {"success": True, "output": json.dumps(pool.list(), indent=2), "data": pool.list()}


def register_ssh_tools(registry):
    """Register SSH tool to registry."""
    registry.register_from_def(
        "ssh_run",
        "at remotehostexecute SSH command. Supports password and key authentication. Must register host first.",
        _ssh_run_handler,
        parameters={
            "host": {"type": "string", "description": "hostname (e.g., my-server)"},
            "command": {"type": "string", "description": "command to execute"},
            "timeout": {"type": "integer", "description": "timeout seconds", "default": 30},
        },
        examples=['ssh_run(host="my-server", command="uptime")',
                  'ssh_run(host="my-server", command="ls -la ~/worldwave")'],
        category="ssh",
    )

    registry.register_from_def(
        "ssh_copy",
        "at SSH host copyfile.",
        _ssh_copy_handler,
        parameters={
            "host": {"type": "string", "description": "hostname"},
            "source": {"type": "string", "description": "sourcepath"},
            "destination": {"type": "string", "description": "goalpath"},
            "direction": {"type": "string", "description": "to (local→remote) or from (remote→local)", "default": "to"},
        },
        category="ssh",
    )

    registry.register_from_def(
        "ssh_test",
        "test and  SSH host connection (ping + hostname) . ",
        _ssh_test_handler,
        parameters={
            "host": {"type": "string", "description": "hostname"},
        },
        category="ssh",
    )

    registry.register_from_def(
        "ssh_hosts",
        "List all registered SSH hosts.",
        _ssh_hosts_handler,
        parameters={},
        category="ssh",
    )
