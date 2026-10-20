"""Start the gateway listener with the Webapp on or off; report what a browser and a native client get.

Run as ``python webapp_listener_peer.py on|off <port> <dist>`` with an isolated ``HERMES_HOME``. Writes
``receipt.json`` there (the gateway redirects stdout). No external inference: the model points at a closed
port and no turn is submitted.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import traceback

SEEDED_TOKEN = "seeded-browser-credential-from-an-earlier-0"  # 43 URL-safe characters


async def probe(mode, port, dist):
    import httpx
    from websockets.asyncio.client import connect

    from gateway.control_socket import GatewayControlServer
    from gateway.run import GatewayRunner
    from gateway.run_api import stop_gateway_api
    from gateway.run_runtime import start_gateway_runtime_api
    from gateway.session_authority import initialize_session_authority
    from gateway.webapp_listener import TOKEN_PATH
    import hermes_cli.webapp
    from tests.gateway.fixtures.local_recovery_probe import rpc

    home = Path(os.environ["HERMES_HOME"])
    webapp_block = f"webapp:\n  enabled: true\n  port: {port}\n" if mode == "on" else ""
    (home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: false\nmodel:\n  provider: custom\n  default: no-inference\n"
        "  base_url: http://127.0.0.1:1/v1\nauxiliary:\n  title_generation:\n    enabled: false\n" + webapp_block)
    if mode == "on":
        (home / TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
        (home / TOKEN_PATH).write_text(SEEDED_TOKEN + "\n", encoding="utf-8")
    hermes_cli.webapp.webapp_dist_dir = lambda _root: Path(dist)  # the built bundle, without a build
    runner = GatewayRunner()
    runner.session_control_server = GatewayControlServer(home)  # verbs only; not listening
    # Set by the gateway bootstrap, which this fixture skips; the gateway is up.
    runner.session_runtime_descriptor = {"runtime_protocol": 1, "state": "ready", "capabilities": [],
                                         "served_profiles": []}
    await initialize_session_authority(runner, profile_id=str(home), instance_id="webapp-listener")
    await start_gateway_runtime_api(runner)
    api = runner.session_api
    from hermes_cli import web_server

    def ask(verb):
        raw = json.dumps({"protocol": 1, "verb": verb, "id": 1, "params": {}}).encode()
        return json.loads(runner.session_control_server.handle_request_line(raw, "fixture-same-user"))

    report = {"api_origin": api.api_origin, "status": ask("webapp-status").get("result")}
    origin = api.api_origin
    try:
        async with httpx.AsyncClient(base_url=origin, timeout=20) as client:
            index = await client.get("/")
            report["index_status"] = index.status_code
            report["index_is_webapp"] = "WEBAPP_BUNDLE" in index.text
            report["index_carries_token"] = web_server._SESSION_TOKEN in index.text
            report["surface"] = getattr(web_server.app.state, "ui_surface", None)
            report["host_terminals"] = hasattr(web_server.app.state, "host_terminals")
            report["session_token_is_seeded"] = web_server._SESSION_TOKEN == SEEDED_TOKEN
            report["token_file"] = (home / TOKEN_PATH).exists()
            minted = await client.post("/api/webapp/gateway-ticket",
                                       headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN})
            report["ticket_status"] = minted.status_code
            if minted.status_code == 200:
                ticket = minted.json()["ticket"]
                url = origin.replace("http:", "ws:") + "/api/ws?native_dial=browser"
                async with connect(url, origin=origin,
                                   subprotocols=["hermes-gateway-v1", f"hermes-gateway-ticket.{ticket}"]) as ws:
                    created = await rpc(ws, "session.create", request_id="browser-create", source="gui",
                                        toolsets=[])
                    report["browser_session"] = "result" in created
            launched = ask("webapp-launch")
            report["launch_ok"] = launched["ok"]
            if launched["ok"]:
                url = launched["result"]["url"]
                report["launch_page"] = url.split("#", 1)[0] == origin + "/webapp/launch"
                report["launch_private"] = launched["result"]["private"]
                ticket = url.split("#ticket=", 1)[1]
                handoff = {"X-Hermes-Window-Ticket": ticket, "Origin": origin}
                redeemed = await client.post("/webapp/window-session", headers=handoff)
                report["handoff_status"] = redeemed.status_code
                report["handoff_gives_credential"] = redeemed.status_code == 200 and (
                    redeemed.json()["token"] == SEEDED_TOKEN)
                report["handoff_reuse_status"] = (
                    await client.post("/webapp/window-session", headers=handoff)).status_code
                report["sign_out_ok"] = ask("webapp-sign-out")["ok"]
                stale = await client.post("/api/webapp/gateway-ticket",
                                          headers={"X-Hermes-Session-Token": SEEDED_TOKEN})
                report["old_credential_status"] = stale.status_code
                report["token_file_rotated"] = (home / TOKEN_PATH).read_text().strip() not in {"", SEEDED_TOKEN}
    finally:
        await stop_gateway_api(api)
    report["host_terminals_after_stop"] = hasattr(web_server.app.state, "host_terminals")
    return report


def main():
    receipt = Path(os.environ["HERMES_HOME"]) / "receipt.json"
    try:
        receipt.write_text(json.dumps(asyncio.run(probe(sys.argv[1], int(sys.argv[2]), sys.argv[3]))))
    except BaseException:
        receipt.write_text(json.dumps({"error": traceback.format_exc()}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
