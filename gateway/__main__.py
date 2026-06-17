#!/usr/bin/env python3
"""Wavegate CLI — launch the Control Plane daemon.

Usage:
    gateway-server --agent-addr localhost:9301 --gateway-port 9302
    gateway-server  # uses defaults from env vars

Environment variables:
    WAVEGATE_AGENT_ADDR  — WW Core Agent gRPC address (default: localhost:9301)
    WAVEGATE_PORT        — Wavegate gRPC listen port (default: 9302)
    WAVEGATE_MAX_WORKERS — thread pool size (default: 10)
"""

import argparse
import asyncio
import logging

from gateway.server import WavegateServer, WavegateConfig


def main():
    parser = argparse.ArgumentParser(
        description="Wavegate — Worldwave Control Plane",
    )
    parser.add_argument(
        "--agent-addr", default="localhost:9301",
        help="WW Core Agent gRPC address (default: localhost:9301)",
    )
    parser.add_argument(
        "--gateway-port", type=int, default=9302,
        help="Wavegate gRPC listen port (default: 9302)",
    )
    parser.add_argument(
        "--max-workers", type=int, default=10,
        help="Thread pool size (default: 10)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = WavegateConfig(
        agent_addr=args.agent_addr,
        gateway_port=args.gateway_port,
        max_workers=args.max_workers,
    )

    server = WavegateServer(config)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\nWavegate stopped.")


if __name__ == "__main__":
    main()
