"""Run pi package management with exactly the same isolation as pi RPC."""

import asyncio
import os
import signal
from contextlib import suppress

from sandbox import isolation


async def run_isolated(root, uid, argv, timeout=120):
    process = await asyncio.create_subprocess_exec(
        *isolation.command(root, argv),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=isolation.environment(),
        user=uid,
        group=uid,
        extra_groups=[],
        preexec_fn=isolation.limits,
        start_new_session=True,
    )
    output = bytearray()

    async def drain():
        while chunk := await process.stdout.read(4096):
            output.extend(chunk)
            if len(output) > 64000:
                del output[:-64000]

    reader = asyncio.create_task(drain())
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
            await reader
        text = output.decode(errors="replace")
        text = isolation.redact(text)
        return {"exit_code": process.returncode, "output": text}
    finally:
        # Also kill package install hooks that detached but remain in this PID namespace.
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
