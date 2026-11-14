import { NextResponse } from "next/server";
import crypto from "crypto";
import { spawn } from "child_process";
import path from "path";
import fs from "fs";

export const dynamic = "force-dynamic";

interface TokenRequest {
  room_name?: string;
  caller?: {
    firstName?: string;
    lastName?: string;
    email?: string;
    mobile?: string;
    reason?: string;
  };
}

function createLiveKitJwt({
  apiKey,
  apiSecret,
  identity,
  name,
  roomName,
  ttlSeconds = 3600 * 6,
}: {
  apiKey: string;
  apiSecret: string;
  identity: string;
  name: string;
  roomName: string;
  ttlSeconds?: number;
}): string {
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "HS256", typ: "JWT" };
  const payload = {
    exp: now + ttlSeconds,
    nbf: now - 5,
    iss: apiKey,
    sub: identity,
    name: name,
    video: {
      room: roomName,
      roomJoin: true,
      canPublish: true,
      canSubscribe: true,
      canPublishData: true,
    },
  };

  const b64Header = Buffer.from(JSON.stringify(header)).toString("base64url");
  const b64Payload = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const signature = crypto
    .createHmac("sha256", apiSecret)
    .update(`${b64Header}.${b64Payload}`)
    .digest("base64url");

  return `${b64Header}.${b64Payload}.${signature}`;
}

export async function POST(req: Request) {
  try {
    const body = (await req.json().catch(() => ({}))) as TokenRequest;
    const roomName =
      body.room_name || `emma-room-${Math.random().toString(36).slice(2, 9)}`;
    const firstName = body.caller?.firstName || "Caller";

    // 1. If an external backend gateway (e.g. Render) is available and not localhost, forward to it
    const remoteGateway =
      process.env.RENDER_GATEWAY_URL ||
      process.env.VOICE_AGENT_URL ||
      process.env.NEXT_PUBLIC_VOICE_GATEWAY_HTTP ||
      "https://voiceassistant-406s.onrender.com";

    const isRemote =
      remoteGateway &&
      !remoteGateway.includes("localhost") &&
      !remoteGateway.includes("127.0.0.1");

    if (isRemote) {
      try {
        const remoteUrl = `${remoteGateway.replace(/\/+$/, "")}/api/voice/token`;
        console.log(`[Token API] Forwarding call token request to Render: ${remoteUrl}`);
        const agentRes = await fetch(remoteUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, room_name: roomName }),
        });

        if (agentRes.ok) {
          const data = await agentRes.json();
          return NextResponse.json(data);
        } else {
          const errData = await agentRes.json().catch(() => ({}));
          console.warn(
            `[Token API] Render gateway responded with status ${agentRes.status}:`,
            errData,
          );
          // If remote gateway failed with a specific detail (e.g. missing API keys), propagate it
          if (errData?.detail || errData?.error) {
            return NextResponse.json(
              { error: errData.detail || errData.error },
              { status: agentRes.status },
            );
          }
        }
      } catch (proxyError) {
        console.error("[Token API] Failed to reach Render voice gateway:", proxyError);
      }
    }

    // 2. Local token generation (for local development or if Render is bypassed)
    const identity = `caller-${Math.random().toString(36).slice(2, 9)}`;
    const apiKey =
      process.env.LIVEKIT_API_KEY ||
      process.env.NEXT_PUBLIC_LIVEKIT_API_KEY ||
      "emma_local";
    const apiSecret = process.env.LIVEKIT_API_SECRET;
    if (!apiSecret) {
      console.error("[Token API] LIVEKIT_API_SECRET is not configured");
      return NextResponse.json(
        {
          error:
            "LIVEKIT_API_SECRET is not configured on the server, and Render gateway was unavailable.",
        },
        { status: 500 },
      );
    }
    const livekitUrl =
      process.env.NEXT_PUBLIC_LIVEKIT_URL ||
      process.env.LIVEKIT_URL ||
      "ws://127.0.0.1:7880";

    const token = createLiveKitJwt({
      apiKey,
      apiSecret,
      identity,
      name: firstName,
      roomName,
    });

    // Start the EMMA agent for this room if running locally.
    const projectRoot = path.resolve(process.cwd());
    const repoRoot = fs.existsSync(path.join(projectRoot, "scripts", "run_agent_session.py"))
      ? projectRoot
      : path.resolve(projectRoot, "..");
    const scriptPath = path.join(repoRoot, "scripts", "run_agent_session.py");

    let agentPid: number | null = null;

    if (fs.existsSync(scriptPath)) {
      const venvPython = path.join(repoRoot, ".venv-313", "bin", "python");
      const pythonBin = fs.existsSync(venvPython) ? venvPython : "python3";

      try {
        console.log(
          `[Token API] Spawning EMMA agent for room '${roomName}' with caller:`,
          body.caller,
        );
        const logDir = path.join(repoRoot, "logs");
        fs.mkdirSync(logDir, { recursive: true });
        const logFd = fs.openSync(path.join(logDir, "agent_session.log"), "a");

        const agentEnv = {
          ...process.env,
          PYTHONPATH: path.join(repoRoot, "src"),
          LIVEKIT_URL: livekitUrl,
          LIVEKIT_API_KEY: apiKey,
          LIVEKIT_API_SECRET: apiSecret,
          PYTHONUNBUFFERED: "1",
        };

        const agentProcess = spawn(
          pythonBin,
          [scriptPath, "--room", roomName, "--caller", JSON.stringify(body.caller || {})],
          {
            cwd: repoRoot,
            env: agentEnv,
            detached: true,
            stdio: ["ignore", logFd, logFd],
          },
        );
        agentProcess.unref();
        agentPid = agentProcess.pid ?? null;

        agentProcess.on("error", (spawnError) => {
          console.error("[Token API] Agent process failed:", spawnError);
        });

        fs.closeSync(logFd);
      } catch (spawnError) {
        console.error("[Token API] Error spawning agent session:", spawnError);
      }
    } else {
      console.log(
        "[Token API] Local agent script not present. Token issued for room: " + roomName,
      );
    }

    return NextResponse.json({
      token,
      livekit_url: livekitUrl,
      room_name: roomName,
      agent_pid: agentPid,
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to mint token";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
