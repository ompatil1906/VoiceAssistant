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
    const identity = `caller-${Math.random().toString(36).slice(2, 9)}`;

    const apiKey =
      process.env.LIVEKIT_API_KEY ||
      process.env.NEXT_PUBLIC_LIVEKIT_API_KEY ||
      "emma_local";
    // A signing secret must come from the environment. Falling back to a value
    // in the source tree would make every checkout share one room secret.
    const apiSecret = process.env.LIVEKIT_API_SECRET;
    if (!apiSecret) {
      console.error("[Token API] LIVEKIT_API_SECRET is not configured");
      return NextResponse.json(
        { error: "LIVEKIT_API_SECRET is not configured on the server" },
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

    // Start the EMMA agent for this room. Without it the browser would join a
    // call that nobody answers, so a missing script is reported as a failure
    // rather than a silent dead line.
    const projectRoot = path.resolve(process.cwd());
    const repoRoot = fs.existsSync(path.join(projectRoot, "scripts", "run_agent_session.py"))
      ? projectRoot
      : path.resolve(projectRoot, "..");
    const scriptPath = path.join(repoRoot, "scripts", "run_agent_session.py");

    if (!fs.existsSync(scriptPath)) {
      console.error(`[Token API] Agent script not found at ${scriptPath}`);
      return NextResponse.json(
        { error: "Voice agent is not installed on this machine" },
        { status: 503 },
      );
    }

    const venvPython = path.join(repoRoot, ".venv-313", "bin", "python");
    const pythonBin = fs.existsSync(venvPython) ? venvPython : "python3";
    let agentPid: number | null = null;

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

      // The child holds its own copy of the descriptor; the parent must not
      // leak one per call.
      fs.closeSync(logFd);
    } catch (spawnError) {
      console.error("[Token API] Error spawning agent session:", spawnError);
      return NextResponse.json(
        { error: "Could not start the voice agent" },
        { status: 503 },
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
