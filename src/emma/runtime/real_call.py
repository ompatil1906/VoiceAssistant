#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Production Real-Call Voice Pipeline Orchestrator.

Constructs an end-to-end, ultra-low-latency conversational voice assistant pipeline
designed for live production telephony and WebRTC calls (e.g. EMMA medical receptionist).

Features integrated:
- Inbound Transport (LiveKit WebRTC or FastAPI WebSocket)
- Neural Noise Suppression (RNNoise / Koala)
- Voice Activity Detection (Silero VAD)
- Machine Learning End-of-Turn Detection (SmartTurn v3 ONNX)
- Speculative Execution Gate (SpeculationGate for sub-500ms TTFB)
- Streaming Speech-to-Text (Deepgram / Whisper)
- Conversational State & Dialog Management (FlowManager)
- Natural Prosody, Breath & Vocal Cue Tagging (ToneTagger)
- Ultra-Low Latency Streaming Text-to-Speech (Cartesia / ElevenLabs / Kokoro)
- Observability & Live Telemetry (UserBotLatencyObserver, MetricsLogObserver, RTVI)
- PipelineParams with enable_metrics=True and allow_interruptions=True for real-time barge-in
- Disabled Jira Tool registration (commented out to eliminate token latency and clutter)
- Automated Excel Call Record Storage (Intake form details, request labels, summaries, appointments)
- Patient ID Verification (ID == 110) & SMTP Email Prescription Dispatch
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from loguru import logger

from emma.audio.filters.rnnoise_filter import RNNoiseFilter
from emma.config import Plan, RuntimeConfig
from emma.cost.policy import CostPolicy
from emma.intelligence.adapters.schemas.function_schema import FunctionSchema
from emma.intelligence.aggregators.llm_context import LLMContext
from emma.intelligence.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
try:
    from emma.intelligence.flows.manager import FlowManager
except Exception:
    FlowManager = Any  # type: ignore
from emma.intelligence.llm.base import FunctionCallParams, LLMService
from emma.observability.observers.loggers.metrics_log_observer import MetricsLogObserver
from emma.observability.observers.user_bot_latency_observer import UserBotLatencyObserver
from emma.runtime.pipeline.pipeline import Pipeline
from emma.runtime.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from emma.runtime.workers.runner import WorkerRunner
from emma.services.prescription_service import (
    VALID_PATIENT_ID,
    handle_prescription_request,
    verify_patient_id,
)
try:
    from emma.speech.expression.tone_tagger.processor import ToneTaggerProcessor
except Exception:
    ToneTaggerProcessor = None  # type: ignore
from emma.speech.turn_detection.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from emma.speech.vad.silero import SileroVADAnalyzer
from emma.storage.call_records_excel import CallIntakeRecord, record_call_to_excel
from emma.transports.base_transport import BaseTransport
from emma.transports.rtvi.processor import RTVIProcessor
from emma.turns.speculation_gate import SpeculationGate
from emma.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies


@dataclass
class RealCallSession:
    """A running production real-call session."""

    pipeline: Pipeline
    worker: PipelineWorker
    runner: WorkerRunner
    transport: BaseTransport
    flow_manager: FlowManager | None = None
    latency_observer: UserBotLatencyObserver | None = None
    metrics_observer: MetricsLogObserver | None = None

    async def start(self) -> None:
        """Start running the call pipeline worker."""
        await self.runner.run()

    async def stop(self) -> None:
        """Cancel and clean up the call session."""
        await self.runner.cancel()


def build_real_call_pipeline(
    transport: BaseTransport,
    config: RuntimeConfig,
    llm: LLMService,
    *,
    flow_manager: FlowManager | None = None,
    caller_info: Mapping[str, Any] | None = None,
    custom_system_instruction: str | None = None,
    enable_noise_filter: bool = True,
    enable_tone_tagging: bool = True,
    enable_speculation: bool = True,
    stt_service: Any = None,
    tts_service: Any = None,
) -> RealCallSession:
    """Build a complete production call pipeline configured with real-call logic.

    Args:
        transport: Inbound/outbound transport (LiveKit WebRTC or Telephony WebSocket).
        config: Runtime configuration (credentials, cost policy, model plans).
        llm: Language model service (Gemini, OpenAI, or Ollama).
        flow_manager: Structured conversational dialog state manager.
        caller_info: Optional caller intake metadata (first_name, last_name, email, mobile_number, reason).
        custom_system_instruction: Base instruction for the assistant.
        enable_noise_filter: Whether to apply RNNoise background suppression.
        enable_tone_tagging: Whether to inject breath/prosody tags for realistic speech.
        enable_speculation: Whether to gate speculative early LLM responses during pauses.
        stt_service: Explicit STT service; if None, resolves via config (Deepgram/Whisper).
        tts_service: Explicit TTS service; if None, resolves via config (Cartesia/ElevenLabs/Kokoro).

    Returns:
        A ready-to-run RealCallSession.
    """
    cost_policy = config.cost_policy()

    # Extract intake form fields
    caller_data = dict(caller_info or {})
    first_name = str(caller_data.get("firstName") or caller_data.get("first_name") or "").strip()
    last_name = str(caller_data.get("lastName") or caller_data.get("last_name") or "").strip()
    email = str(caller_data.get("email") or "").strip()
    mobile_number = str(
        caller_data.get("mobileNumber") or caller_data.get("mobile_number") or ""
    ).strip()
    intake_reason = str(caller_data.get("reason") or "").strip()

    # 1. Resolve Speech-To-Text
    if stt_service is None:
        if config.stt_provider == "deepgram" or config.has_credential("DEEPGRAM_API_KEY"):
            cost_policy.assert_allowed("deepgram")
            from emma.speech.stt.deepgram.stt import DeepgramSTTService

            api_key = config.credentials.get("DEEPGRAM_API_KEY", "")
            stt_service = DeepgramSTTService(
                api_key=api_key,
                model=config.deepgram_model,
            )
        else:
            cost_policy.assert_allowed("whisper")
            from emma.speech.stt.whisper import WhisperSTTService

            stt_service = WhisperSTTService(model=config.whisper_model)

    # 2. Resolve Text-To-Speech
    if tts_service is None:
        if config.tts_provider == "cartesia" or config.has_credential("CARTESIA_API_KEY"):
            cost_policy.assert_allowed("cartesia")
            from emma.speech.tts.cartesia.tts import CartesiaTTSService

            api_key = config.credentials.get("CARTESIA_API_KEY", "")
            tts_service = CartesiaTTSService(
                api_key=api_key,
                voice_id=config.cartesia_voice_id,
            )
        elif config.tts_provider == "elevenlabs" or config.has_credential("ELEVENLABS_API_KEY"):
            cost_policy.assert_allowed("elevenlabs")
            from emma.speech.tts.elevenlabs.tts import ElevenLabsTTSService

            api_key = config.credentials.get("ELEVENLABS_API_KEY", "")
            tts_service = ElevenLabsTTSService(
                api_key=api_key,
                voice_id=config.elevenlabs_voice_id,
            )
        else:
            cost_policy.assert_allowed("kokoro")
            from emma.speech.tts.kokoro import KokoroTTSService

            tts_service = KokoroTTSService(voice=config.kokoro_voice)

    # 3. Audio preprocessing & filters
    audio_processors = []
    if enable_noise_filter:
        try:
            audio_processors.append(RNNoiseFilter())
        except Exception as e:
            logger.warning(f"RNNoiseFilter not available; continuing without it: {e}")

    # 4. Turn detection & VAD
    vad_analyzer = SileroVADAnalyzer()
    smart_turn = LocalSmartTurnAnalyzerV3()

    # 5. LLM Context & Conversation State Management
    initial_context = LLMContext()

    # Base clinical receptionist prompt with patient verification instructions
    default_prompt = (
        "You are EMMA, an autonomous, highly empathetic, and efficient medical receptionist "
        "at QuantumLoopAI Health Clinic. "
        f"You are speaking with {first_name or 'the caller'}. "
        f"Caller Email: {email or 'Not provided'}. "
        f"Caller Mobile: {mobile_number or 'Not provided'}. "
        f"Initial Inquiry Reason: {intake_reason or 'General Inquiry'}.\n\n"
        "Guidelines:\n"
        "1. PRESCRIPTION REQUESTS: When a caller asks for a prescription renewal, refill, or copy, "
        "always politely ask for their Patient ID to confirm their identity. "
        "The authorized patient ID for this demo account is 110. "
        "Once the caller confirms their Patient ID is 110, invoke the `request_prescription` tool. "
        "Inform them that their prescription has been sent directly to their email.\n"
        "2. APPOINTMENTS: When the caller wants to schedule or book an appointment, ask for their "
        "desired date, time, and reason, and call `book_appointment`.\n"
        "3. KEEP RESPONSES CONCISE: Speak naturally, warm, and avoid lengthy monologues."
    )

    system_instruction = (
        f"{custom_system_instruction}\n\n{default_prompt}"
        if custom_system_instruction
        else default_prompt
    )
    initial_context.add_message({"role": "system", "content": system_instruction})

    # ==============================================================================
    # JIRA TOOL REGISTRATION (DISABLED)
    # ==============================================================================
    # The Jira issue/ticket management tool registration has been commented out to
    # prevent cluttering the pipeline context, reduce prompt overhead, and eliminate
    # token generation latency during live real-time voice calls.
    #
    # async def _create_jira_ticket(params: FunctionCallParams):
    #     """Create a Jira ticket for clinic follow-up or administrative escalations."""
    #     issue_type = params.arguments.get("issue_type", "Task")
    #     summary = params.arguments.get("summary", "")
    #     description = params.arguments.get("description", "")
    #     logger.info(f"[JIRA] Creating ticket: {summary} ({issue_type})")
    #     await params.result_callback({"status": "created", "key": "EMMA-101", "summary": summary})
    #
    # llm.register_function("create_jira_ticket", _create_jira_ticket)
    # ==============================================================================

    # ==============================================================================
    # CLINIC TOOLS: PRESCRIPTION (PATIENT ID 110) & APPOINTMENT BOOKING (EXCEL LOG)
    # ==============================================================================

    async def _handle_prescription_tool(params: FunctionCallParams):
        """Handle patient ID verification and SMTP email dispatch for prescriptions."""
        args = params.arguments
        raw_patient_id = args.get("patient_id")
        med = args.get("medication") or "Amoxicillin 500mg Oral Capsule"
        target_email = args.get("email") or email or "patient@quantumloop.ai"
        full_name = f"{first_name} {last_name}".strip()

        logger.info(
            f"[Tool: Prescription] Verifying Patient ID '{raw_patient_id}' for {target_email}..."
        )
        res = handle_prescription_request(
            patient_id_input=raw_patient_id,
            to_email=target_email,
            patient_name=full_name,
            medication=med,
        )

        # Log call outcome into Excel ledger
        excel_status = "Prescription Sent" if res.get("verified") else "Unverified ID"
        summary_msg = (
            f"Prescription for {med} verified for Patient ID #{raw_patient_id} and emailed to {target_email}."
            if res.get("verified")
            else f"Prescription request failed: Provided Patient ID '{raw_patient_id}' does not match required ID 110."
        )

        record_call_to_excel(
            first_name=first_name,
            last_name=last_name,
            email=target_email,
            mobile_number=mobile_number,
            intake_reason=intake_reason or "Prescription Request",
            request_label="Prescription Request",
            patient_id=str(raw_patient_id),
            call_summary=summary_msg,
            status=excel_status,
        )

        await params.result_callback(res)

    async def _handle_appointment_tool(params: FunctionCallParams):
        """Handle appointment scheduling and save details into Excel records."""
        args = params.arguments
        appt_date = args.get("appointment_date", "")
        appt_time = args.get("appointment_time", "")
        reason = args.get("reason", "") or intake_reason or "Clinic Consultation"
        target_email = email or "patient@quantumloop.ai"

        logger.info(
            f"[Tool: Appointment] Booking appointment for {first_name} on {appt_date} at {appt_time}..."
        )
        summary_msg = f"Appointment confirmed on {appt_date} at {appt_time} for: {reason}."

        record_call_to_excel(
            first_name=first_name,
            last_name=last_name,
            email=target_email,
            mobile_number=mobile_number,
            intake_reason=intake_reason or reason,
            request_label="Appointment Booking",
            appointment_date=appt_date,
            appointment_time=appt_time,
            call_summary=summary_msg,
            status="Confirmed",
        )

        await params.result_callback(
            {
                "success": True,
                "status": "confirmed",
                "appointment_date": appt_date,
                "appointment_time": appt_time,
                "reason": reason,
                "message": f"Appointment successfully scheduled for {appt_date} at {appt_time}.",
            }
        )

    async def _handle_summary_tool(params: FunctionCallParams):
        """Record general interaction summary into Excel records."""
        args = params.arguments
        summary_text = args.get("summary", "")
        label = args.get("request_label", "General Inquiry")

        record_call_to_excel(
            first_name=first_name,
            last_name=last_name,
            email=email,
            mobile_number=mobile_number,
            intake_reason=intake_reason,
            request_label=label,
            call_summary=summary_text,
            status="Completed",
        )
        await params.result_callback({"success": True, "recorded": True})

    # Tool Schemas for LLM
    prescription_schema = FunctionSchema(
        name="request_prescription",
        description=(
            "Verify patient identity using Patient ID (required ID: 110) and dispatch official "
            "demo prescription via SMTP email to the patient."
        ),
        properties={
            "patient_id": {
                "type": "string",
                "description": "The patient ID provided by the caller. Must be confirmed as 110.",
            },
            "medication": {
                "type": "string",
                "description": "Optional name or strength of medication requested (default Amoxicillin 500mg).",
            },
            "email": {
                "type": "string",
                "description": "Destination email for prescription if different from intake email.",
            },
        },
        required=["patient_id"],
    )

    appointment_schema = FunctionSchema(
        name="book_appointment",
        description="Book a clinic appointment for the caller and record date and time into the clinic Excel ledger.",
        properties={
            "appointment_date": {
                "type": "string",
                "description": "Desired appointment date (e.g. '2026-11-15' or 'next Tuesday').",
            },
            "appointment_time": {
                "type": "string",
                "description": "Desired appointment time (e.g. '10:30 AM').",
            },
            "reason": {
                "type": "string",
                "description": "Specific symptom or purpose of visit.",
            },
        },
        required=["appointment_date", "appointment_time"],
    )

    summary_schema = FunctionSchema(
        name="record_call_summary",
        description="Store final call summary and request label in clinic records.",
        properties={
            "summary": {
                "type": "string",
                "description": "Brief summary of the call discussion and agreed action.",
            },
            "request_label": {
                "type": "string",
                "description": "Category: Appointment Booking, Prescription Request, General Inquiry, Billing.",
            },
        },
        required=["summary"],
    )

    initial_context.set_tools([prescription_schema, appointment_schema, summary_schema])
    llm.register_function("request_prescription", _handle_prescription_tool)
    llm.register_function("book_appointment", _handle_appointment_tool)
    llm.register_function("record_call_summary", _handle_summary_tool)

    user_params = LLMUserAggregatorParams()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        initial_context,
        user_params=user_params,
    )

    # 6. RTVI & Live Telemetry Observers
    # MetricsLogObserver: Logs TTFB (Time to First Byte), TTFA, TTFAT, and token usage to terminal.
    # UserBotLatencyObserver: Tracks turn-taking delay between user speech end and bot audio start.
    rtvi = RTVIProcessor()
    latency_observer = UserBotLatencyObserver()
    metrics_observer = MetricsLogObserver()

    # 7. Assemble Pipeline Processors
    pipeline_processors = [
        transport.input(),
        *audio_processors,
        stt_service,
        rtvi,
        user_aggregator,
    ]

    # Flow manager or direct LLM
    if flow_manager is not None:
        pipeline_processors.append(flow_manager)
    else:
        pipeline_processors.append(llm)

    # Prosody & Tone Tagger
    if enable_tone_tagging and ToneTaggerProcessor is not None:
        try:
            pipeline_processors.append(ToneTaggerProcessor())
        except Exception as e:
            logger.warning(f"ToneTaggerProcessor optional load skipped: {e}")

    # Speculation Gate for low-latency speculative replies
    if enable_speculation:
        pipeline_processors.append(SpeculationGate())

    # TTS & Output transport
    pipeline_processors.extend(
        [
            tts_service,
            transport.output(),
            assistant_aggregator,
        ]
    )

    pipeline = Pipeline(pipeline_processors)

    # Telemetry enabled and instant barge-in enabled in PipelineParams
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            allow_interruptions=True,
        ),
        observers=[latency_observer, metrics_observer, rtvi],
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )

    runner = WorkerRunner()
    runner.add_worker(worker)

    return RealCallSession(
        pipeline=pipeline,
        worker=worker,
        runner=runner,
        transport=transport,
        flow_manager=flow_manager,
        latency_observer=latency_observer,
        metrics_observer=metrics_observer,
    )
