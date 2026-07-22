import os
import asyncio
import base64
import json
import boto3
import uuid
import warnings
import pyaudio
import pytz
import random
import hashlib
import datetime
import time
import inspect
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamInputChunk,
    BidirectionalInputPayloadPart,
)
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver
from decimal import Decimal
from boto3.dynamodb.conditions import Attr

# Suppress warnings
warnings.filterwarnings("ignore")

# Audio configuration
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK_SIZE = 1024  # Number of frames per buffer

# Debug mode flag
DEBUG = False


def debug_print(message):
    """Print only if debug mode is enabled"""
    if DEBUG:
        functionName = inspect.stack()[1].function
        if functionName == "time_it" or functionName == "time_it_async":
            functionName = inspect.stack()[2].function
        print(
            "{:%Y-%m-%d %H:%M:%S.%f}".format(datetime.datetime.now())[:-3]
            + " "
            + functionName
            + " "
            + message
        )


def time_it(label, methodToRun):
    start_time = time.perf_counter()
    result = methodToRun()
    end_time = time.perf_counter()
    debug_print(f"Execution time for {label}: {end_time - start_time:.4f} seconds")
    return result


async def time_it_async(label, methodToRun):
    start_time = time.perf_counter()
    result = await methodToRun()
    end_time = time.perf_counter()
    debug_print(f"Execution time for {label}: {end_time - start_time:.4f} seconds")
    return result


class ToolProcessor:
    def __init__(self):
        # ThreadPoolExecutor could be used for complex implementations
        self.tasks = {}
        self.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        self.guest_table = self.dynamodb.Table("Hotel_Guests")
        self.reservation_table = self.dynamodb.Table("Hotel_Reservations")

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()

    async def process_tool_async(self, tool_name, tool_content):
        """Process a tool call asynchronously and return the result"""
        # Create a unique task ID
        task_id = str(uuid.uuid4())

        # Create and store the task
        task = asyncio.create_task(self._run_tool(tool_name, tool_content))
        self.tasks[task_id] = task

        try:
            # Wait for the task to complete
            result = await task
            return result
        finally:
            # Clean up the task reference
            if task_id in self.tasks:
                del self.tasks[task_id]

    async def _run_tool(self, tool_name, tool_content):
        """Internal method to execute the tool logic"""
        debug_print(f"Processing tool: {tool_name}")
        tool = tool_name.lower()
        content = tool_content.get("content", {})
        if isinstance(content, str):
            try:
                content_data = json.loads(content)
            except json.JSONDecodeError:
                content_data = {}
        else:
            content_data = content

        if tool == "checkguestprofiletool":
            # Look up the guest in Hotel_Guests
            return await self.loop.run_in_executor(
                None,
                self._execute_check_guest,
                content_data
            )
        elif tool == "checkreservationstatustool":
            # Look up upcoming and past reservations for this guest
            return await self.loop.run_in_executor(
                None,
                self._execute_check_reservation_status,
                content_data
            )
        elif tool == "updatereservationtool":
            # Update room type and/or special requests
            return await self.loop.run_in_executor(
                None,
                self._execute_update_reservation,
                content_data
            )
        else:
            return {"error": f"Unsupported tool: {tool_name}"}



    
   
    def _execute_check_guest(self, content_data):
        """
        Look up a guest profile in Hotel_Guests by guestName.
        Used for identity verification and preferences.
        """
        try:
            guest_name = content_data.get("guestName")
            if not guest_name:
                return {"error": "guestName is required."}

            response = self.guest_table.get_item(Key={'guestName': guest_name})

            if 'Item' not in response:
                return {"found": False, "message": "Guest not found."}

            item = response['Item']

            return {
                "found": True,
                "guestName": item['guestName'],
                "dob": item.get('dob'),
                "loyaltyTier": item.get('loyaltyTier'),
                "phoneNumber": item.get('phoneNumber'),
                "email": item.get('email'),
                "preferredLanguage": item.get('preferredLanguage'),
                "preferredBedType": item.get('preferredBedType'),
                "preferredView": item.get('preferredView'),
                "vipFlag": bool(item.get('vipFlag', False))
            }
        except Exception as e:
            return {"error": str(e)}

    def _execute_check_reservation_status(self, content_data):
        """
        Check upcoming and (optionally) past reservations in Hotel_Reservations
        for a given guest.
        """
        try:
            from boto3.dynamodb.conditions import Attr
            import datetime as _dt

            guest_name = content_data.get("guestName")
            include_past = content_data.get("includePastStays", False)

            if not guest_name:
                return {"error": "guestName is required."}

            today_str = _dt.date.today().strftime('%Y-%m-%d')

            # Find all reservations for this guest
            filter_expression = Attr('guestName').eq(guest_name)
            response = self.reservation_table.scan(FilterExpression=filter_expression)
            items = response.get('Items', [])

            if not items:
                return {
                    "found": False,
                    "message": "No reservations found for this guest."
                }

            # Separate upcoming/current vs past stays
            upcoming = []
            past = []

            for r in items:
                check_out = r.get('checkOutDate', '')
                status = r.get('status', '')
                # Normalize Decimal -> string for balanceDue
                if 'balanceDue' in r and isinstance(r['balanceDue'], Decimal):
                    r['balanceDue'] = str(r['balanceDue'])

                if check_out >= today_str and status in ["Confirmed", "CheckedIn"]:
                    upcoming.append(r)
                else:
                    past.append(r)

            # Sort upcoming by checkInDate
            upcoming.sort(key=lambda x: x.get('checkInDate', '9999-99-99'))

            upcoming_res = upcoming[0] if upcoming else None
            past_stays = past if include_past else []

            message_parts = []
            if upcoming_res:
                msg = (
                    f"You have an upcoming stay in room {upcoming_res.get('roomNumber')} "
                    f"({upcoming_res.get('roomType')}) from "
                    f"{upcoming_res.get('checkInDate')} to {upcoming_res.get('checkOutDate')}."
                )
                if upcoming_res.get('paymentStatus') != "Paid":
                    msg += f" Your current balance due is {upcoming_res.get('balanceDue', '0.00')}."
                message_parts.append(msg)
            else:
                message_parts.append("You have no upcoming reservations.")

            if include_past and past_stays:
                message_parts.append(f"I also found {len(past_stays)} past stay(s).")

            return {
                "found": True,
                "upcomingReservation": upcoming_res,
                "pastStays": past_stays,
                "message": " ".join(message_parts)
            }

        except Exception as e:
            return {"error": str(e)}

    def _execute_update_reservation(self, content_data):
        """
        Update a reservation's room type and/or special requests in Hotel_Reservations.
        - reservationId: required
        - newRoomType: optional string
        - newSpecialRequest: optional string (appended to specialRequests list)
        """
        try:
            reservation_id = content_data.get("reservationId")
            new_room_type = content_data.get("newRoomType")
            new_special_request = content_data.get("newSpecialRequest")

            if not reservation_id:
                return {"error": "reservationId is required."}

            # Build dynamic update expression
            update_parts = []
            expr_values = {}

            if new_room_type:
                update_parts.append("roomType = :rt")
                expr_values[":rt"] = new_room_type

            if new_special_request:
                update_parts.append(
                    "specialRequests = list_append("
                    "if_not_exists(specialRequests, :empty_list), :sr)"
                )
                expr_values[":sr"] = [new_special_request]
                expr_values[":empty_list"] = []

            if not update_parts:
                return {
                    "error": "Nothing to update. Provide newRoomType and/or newSpecialRequest."
                }

            update_expression = "SET " + ", ".join(update_parts)

            # Apply update
            self.reservation_table.update_item(
                Key={'reservationId': reservation_id},
                UpdateExpression=update_expression,
                ExpressionAttributeValues=expr_values
            )

            # Fetch updated reservation to return full context
            response = self.reservation_table.get_item(
                Key={'reservationId': reservation_id}
            )
            updated_item = response.get("Item", {})

            # Convert Decimal to string for balanceDue if present
            if 'balanceDue' in updated_item and isinstance(updated_item['balanceDue'], Decimal):
                updated_item['balanceDue'] = str(updated_item['balanceDue'])

            msg_parts = [f"Reservation {reservation_id} has been updated."]
            if new_room_type:
                msg_parts.append(f"New room type: {new_room_type}.")
            if new_special_request:
                msg_parts.append(f"Added special request: '{new_special_request}'.")

            return {
                "success": True,
                "message": " ".join(msg_parts),
                "updatedReservation": updated_item
            }

        except Exception as e:
            return {"error": str(e)}


class BedrockStreamManager:
    """Manages bidirectional streaming with AWS Bedrock using asyncio"""

    # Event templates
    START_SESSION_EVENT = """{
        "event": {
            "sessionStart": {
            "inferenceConfiguration": {
                "maxTokens": 1024,
                "topP": 0.9,
                "temperature": 0.7
                }
            }
        }
    }"""

    CONTENT_START_EVENT = """{
        "event": {
            "contentStart": {
            "promptName": "%s",
            "contentName": "%s",
            "type": "AUDIO",
            "interactive": true,
            "role": "USER",
            "audioInputConfiguration": {
                "mediaType": "audio/lpcm",
                "sampleRateHertz": 16000,
                "sampleSizeBits": 16,
                "channelCount": 1,
                "audioType": "SPEECH",
                "encoding": "base64"
                }
            }
        }
    }"""

    AUDIO_EVENT_TEMPLATE = """{
        "event": {
            "audioInput": {
            "promptName": "%s",
            "contentName": "%s",
            "content": "%s"
            }
        }
    }"""

    TEXT_CONTENT_START_EVENT = """{
        "event": {
            "contentStart": {
            "promptName": "%s",
            "contentName": "%s",
            "type": "TEXT",
            "role": "%s",
            "interactive": false,
                "textInputConfiguration": {
                    "mediaType": "text/plain"
                }
            }
        }
    }"""

    TEXT_INPUT_EVENT = """{
        "event": {
            "textInput": {
            "promptName": "%s",
            "contentName": "%s",
            "content": "%s"
            }
        }
    }"""

    TOOL_CONTENT_START_EVENT = """{
        "event": {
            "contentStart": {
                "promptName": "%s",
                "contentName": "%s",
                "interactive": false,
                "type": "TOOL",
                "role": "TOOL",
                "toolResultInputConfiguration": {
                    "toolUseId": "%s",
                    "type": "TEXT",
                    "textInputConfiguration": {
                        "mediaType": "text/plain"
                    }
                }
            }
        }
    }"""

    CONTENT_END_EVENT = """{
        "event": {
            "contentEnd": {
            "promptName": "%s",
            "contentName": "%s"
            }
        }
    }"""

    PROMPT_END_EVENT = """{
        "event": {
            "promptEnd": {
            "promptName": "%s"
            }
        }
    }"""

    SESSION_END_EVENT = """{
        "event": {
            "sessionEnd": {}
        }
    }"""

    def start_prompt(self):
        """Create a promptStart event"""
        guest_tool_schema = json.dumps(
            {
                "type": "object",
                "properties": {
                    "guestName": {
                        "type": "string",
                        "description": "The full name of the hotel guest.",
                    }
                },
                "required": ["guestName"],
            }
        )

        reservation_tool_schema = json.dumps(
            {
                "type": "object",
                "properties": {
                    "guestName": {
                        "type": "string",
                        "description": "The full name of the hotel guest.",
                    },
                    "includePastStays": {
                        "type": "boolean",
                        "description": "If true, also return past stays.",
                        "default": False,
                    },
                },
                "required": ["guestName"],
            }
        )

        update_reservation_tool_schema = json.dumps(
            {
                "type": "object",
                "properties": {
                    "reservationId": {
                        "type": "string",
                        "description": "The reservation ID to update (e.g., 'RES-1001').",
                    },
                    "newRoomType": {
                        "type": "string",
                        "description": "New room type to set (e.g., 'King Deluxe'). Optional.",
                    },
                    "newSpecialRequest": {
                        "type": "string",
                        "description": "A short note to append to specialRequests, e.g. 'Feather-free pillows'. Optional.",
                    },
                },
                "required": ["reservationId"],
            }
        )

        prompt_start_event = {
            "event": {
                "promptStart": {
                    "promptName": self.prompt_name,
                    "textOutputConfiguration": {"mediaType": "text/plain"},
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 24000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": "matthew",
                        "encoding": "base64",
                        "audioType": "SPEECH",
                    },
                    "toolUseOutputConfiguration": {"mediaType": "application/json"},
                    "toolConfiguration": {
                        "tools": [
                            {
                                "toolSpec": {
                                    "name": "checkGuestProfileTool",
                                    "description": (
                                        "Use this tool to look up a hotel guest's profile in the hotel system. "
                                        "It returns DOB for identity verification, loyalty tier, and preferences."
                                    ),
                                    "inputSchema": {"json": guest_tool_schema},
                                }
                            },
                            {
                                "toolSpec": {
                                    "name": "checkReservationStatusTool",
                                    "description": (
                                        "Use this tool to check the guest's upcoming reservation and, optionally, past stays. "
                                        "Call this after verifying identity to answer questions about bookings or balances."
                                    ),
                                    "inputSchema": {"json": reservation_tool_schema},
                                }
                            },
                            {
                                "toolSpec": {
                                    "name": "updateReservationTool",
                                    "description": (
                                        "Use this tool to update an existing reservation's room type and/or add a special request. "
                                        "Only use it after the guest clearly confirms what they want to change."
                                    ),
                                    "inputSchema": {
                                        "json": update_reservation_tool_schema
                                    },
                                }
                            },
                        ]
                    },
                }
            }
        }

        return json.dumps(prompt_start_event)

    def tool_result_event(self, content_name, content, role):
        """Create a tool result event"""

        if isinstance(content, dict):
            content_json_string = json.dumps(content)
        else:
            content_json_string = content

        tool_result_event = {
            "event": {
                "toolResult": {
                    "promptName": self.prompt_name,
                    "contentName": content_name,
                    "content": content_json_string,
                }
            }
        }
        return json.dumps(tool_result_event)

    def __init__(self, model_id="amazon.nova-sonic-v1:0", region="us-east-1"):
        """Initialize the stream manager."""
        self.model_id = model_id
        self.region = region

        # Replace RxPy subjects with asyncio queues
        self.audio_input_queue = asyncio.Queue()
        self.audio_output_queue = asyncio.Queue()
        self.output_queue = asyncio.Queue()

        self.response_task = None
        self.stream_response = None
        self.is_active = False
        self.barge_in = False
        self.bedrock_client = None

        # Audio playback components
        self.audio_player = None

        # Text response components
        self.display_assistant_text = False
        self.role = None

        # Session information
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.toolUseContent = ""
        self.toolUseId = ""
        self.toolName = ""

        # Add a tool processor
        self.tool_processor = ToolProcessor()

        # Add tracking for in-progress tool calls
        self.pending_tool_tasks = {}

    def _initialize_client(self):
        """Initialize the Bedrock client."""
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
        )
        self.bedrock_client = BedrockRuntimeClient(config=config)

    async def initialize_stream(self):
        """Initialize the bidirectional stream with Bedrock."""
        if not self.bedrock_client:
            self._initialize_client()

        try:
            self.stream_response = await time_it_async(
                "invoke_model_with_bidirectional_stream",
                lambda: self.bedrock_client.invoke_model_with_bidirectional_stream(
                    InvokeModelWithBidirectionalStreamOperationInput(
                        model_id=self.model_id
                    )
                ),
            )
            self.is_active = True
            # default_system_prompt = "You are the AI receptionist for SimplePractice. SECURITY PROTOCOL: You MUST verify the patient identity before checking billing OR scheduling. 1. Ask for their Full Name and Date of Birth. 2. Use checkBillingTool to look them up. 3. Compare the DOB returned by the tool with the DOB the user provided. If they match, proceed. SCHEDULING PROTOCOL: 1. Ask for preferred date and time. 2. Use checkAppointmentAvailabilityTool to see if the doctor is free. 3. If free, use scheduleAppointmentTool to book it. Be warm, empathetic, and professional."
            default_system_prompt = (
                "You are the virtual front desk assistant for a hotel. "
                "Clearly state that you are a virtual assistant who can help guests with questions "
                "about their reservations, balances, and simple changes like room type or special requests."
                "SECURITY:"
                "- Before giving any reservation or billing details, you MUST verify the guest's identity."
                "- Politely ask for their full name and date of birth."
                "- Use checkGuestProfileTool to look them up."
                "- Compare the DOB the guest said with the DOB from the tool. Only continue if they match."
                "AFTER ID VERIFICATION:"
                "1. If they ask about an upcoming stay, room details, or balance, call checkReservationStatusTool "
                "   with their guestName. Use includePastStays=true only if they ask about previous stays."
                "2. If they want to change their room type or add a special request (e.g. extra pillows, "
                "   high floor, feather-free pillows), first identify the correct reservationId using "
                "   checkReservationStatusTool, confirm it with the guest, then call updateReservationTool."
                "3. After updating, explain clearly what changed (e.g. new room type or the special request you added)."
                "STYLE:"
                "- Be warm, professional, and concise."
                "- Confirm important details back to the guest before updating."
                "- Do not invent reservations or balances that are not in the database."
            )

            # Send initialization events
            prompt_event = self.start_prompt()
            text_content_start = self.TEXT_CONTENT_START_EVENT % (
                self.prompt_name,
                self.content_name,
                "SYSTEM",
            )
            text_content = self.TEXT_INPUT_EVENT % (
                self.prompt_name,
                self.content_name,
                default_system_prompt,
            )
            text_content_end = self.CONTENT_END_EVENT % (
                self.prompt_name,
                self.content_name,
            )

            init_events = [
                self.START_SESSION_EVENT,
                prompt_event,
                text_content_start,
                text_content,
                text_content_end,
            ]

            for event in init_events:
                await self.send_raw_event(event)
                # Small delay between init events
                await asyncio.sleep(0.1)

            # Start listening for responses
            self.response_task = asyncio.create_task(self._process_responses())

            # Start processing audio input
            asyncio.create_task(self._process_audio_input())

            # Wait a bit to ensure everything is set up
            await asyncio.sleep(0.1)

            debug_print("Stream initialized successfully")
            return self
        except Exception as e:
            self.is_active = False
            print(f"Failed to initialize stream: {str(e)}")
            raise

    async def send_raw_event(self, event_json):
        """Send a raw event JSON to the Bedrock stream."""
        if not self.stream_response or not self.is_active:
            debug_print("Stream not initialized or closed")
            return

        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
        )

        try:
            await self.stream_response.input_stream.send(event)
            # For debugging large events, you might want to log just the type
            if DEBUG:
                if len(event_json) > 200:
                    event_type = json.loads(event_json).get("event", {}).keys()
                    debug_print(f"Sent event type: {list(event_type)}")
                else:
                    debug_print(f"Sent event: {event_json}")
        except Exception as e:
            debug_print(f"Error sending event: {str(e)}")
            if DEBUG:
                import traceback

                traceback.print_exc()

    async def send_audio_content_start_event(self):
        """Send a content start event to the Bedrock stream."""
        content_start_event = self.CONTENT_START_EVENT % (
            self.prompt_name,
            self.audio_content_name,
        )
        await self.send_raw_event(content_start_event)

    async def _process_audio_input(self):
        """Process audio input from the queue and send to Bedrock."""
        while self.is_active:
            try:
                # Get audio data from the queue
                data = await self.audio_input_queue.get()

                audio_bytes = data.get("audio_bytes")
                if not audio_bytes:
                    debug_print("No audio bytes received")
                    continue

                # Base64 encode the audio data
                blob = base64.b64encode(audio_bytes)
                audio_event = self.AUDIO_EVENT_TEMPLATE % (
                    self.prompt_name,
                    self.audio_content_name,
                    blob.decode("utf-8"),
                )

                # Send the event
                await self.send_raw_event(audio_event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_print(f"Error processing audio: {e}")
                if DEBUG:
                    import traceback

                    traceback.print_exc()

    def add_audio_chunk(self, audio_bytes):
        """Add an audio chunk to the queue."""
        self.audio_input_queue.put_nowait(
            {
                "audio_bytes": audio_bytes,
                "prompt_name": self.prompt_name,
                "content_name": self.audio_content_name,
            }
        )

    async def send_audio_content_end_event(self):
        """Send a content end event to the Bedrock stream."""
        if not self.is_active:
            debug_print("Stream is not active")
            return

        content_end_event = self.CONTENT_END_EVENT % (
            self.prompt_name,
            self.audio_content_name,
        )
        await self.send_raw_event(content_end_event)
        debug_print("Audio ended")

    async def send_tool_start_event(self, content_name, tool_use_id):
        """Send a tool content start event to the Bedrock stream."""
        content_start_event = self.TOOL_CONTENT_START_EVENT % (
            self.prompt_name,
            content_name,
            tool_use_id,
        )
        debug_print(f"Sending tool start event: {content_start_event}")
        await self.send_raw_event(content_start_event)

    async def send_tool_result_event(self, content_name, tool_result):
        """Send a tool content event to the Bedrock stream."""
        # Use the actual tool result from processToolUse
        tool_result_event = self.tool_result_event(
            content_name=content_name, content=tool_result, role="TOOL"
        )
        debug_print(f"Sending tool result event: {tool_result_event}")
        await self.send_raw_event(tool_result_event)

    async def send_tool_content_end_event(self, content_name):
        """Send a tool content end event to the Bedrock stream."""
        tool_content_end_event = self.CONTENT_END_EVENT % (
            self.prompt_name,
            content_name,
        )
        debug_print(f"Sending tool content event: {tool_content_end_event}")
        await self.send_raw_event(tool_content_end_event)

    async def send_prompt_end_event(self):
        """Close the stream and clean up resources."""
        if not self.is_active:
            debug_print("Stream is not active")
            return

        prompt_end_event = self.PROMPT_END_EVENT % (self.prompt_name)
        await self.send_raw_event(prompt_end_event)
        debug_print("Prompt ended")

    async def send_session_end_event(self):
        """Send a session end event to the Bedrock stream."""
        if not self.is_active:
            debug_print("Stream is not active")
            return

        await self.send_raw_event(self.SESSION_END_EVENT)
        self.is_active = False
        debug_print("Session ended")

    async def _process_responses(self):
        """Process incoming responses from Bedrock."""
        try:
            while self.is_active:
                try:
                    output = await self.stream_response.await_output()
                    result = await output[1].receive()
                    if result.value and result.value.bytes_:
                        try:
                            response_data = result.value.bytes_.decode("utf-8")
                            json_data = json.loads(response_data)

                            # Handle different response types
                            if "event" in json_data:
                                if "completionStart" in json_data["event"]:
                                    debug_print(
                                        f"completionStart: {json_data['event']}"
                                    )
                                elif "contentStart" in json_data["event"]:
                                    debug_print("Content start detected")
                                    content_start = json_data["event"]["contentStart"]
                                    # set role
                                    self.role = content_start["role"]
                                    # Check for speculative content
                                    if "additionalModelFields" in content_start:
                                        try:
                                            additional_fields = json.loads(
                                                content_start["additionalModelFields"]
                                            )
                                            if (
                                                additional_fields.get("generationStage")
                                                == "SPECULATIVE"
                                            ):
                                                debug_print(
                                                    "Speculative content detected"
                                                )
                                                self.display_assistant_text = True
                                            else:
                                                self.display_assistant_text = False
                                        except json.JSONDecodeError:
                                            debug_print(
                                                "Error parsing additionalModelFields"
                                            )
                                elif "textOutput" in json_data["event"]:
                                    text_content = json_data["event"]["textOutput"][
                                        "content"
                                    ]
                                    role = json_data["event"]["textOutput"]["role"]
                                    # Check if there is a barge-in
                                    if '{ "interrupted" : true }' in text_content:
                                        debug_print(
                                            "Barge-in detected. Stopping audio output."
                                        )
                                        self.barge_in = True

                                    if (
                                        self.role == "ASSISTANT"
                                        and self.display_assistant_text
                                    ):
                                        print(f"Assistant: {text_content}")
                                    elif self.role == "USER":
                                        print(f"User: {text_content}")
                                elif "audioOutput" in json_data["event"]:
                                    audio_content = json_data["event"]["audioOutput"][
                                        "content"
                                    ]
                                    audio_bytes = base64.b64decode(audio_content)
                                    await self.audio_output_queue.put(audio_bytes)
                                elif "toolUse" in json_data["event"]:
                                    self.toolUseContent = json_data["event"]["toolUse"]
                                    self.toolName = json_data["event"]["toolUse"][
                                        "toolName"
                                    ]
                                    self.toolUseId = json_data["event"]["toolUse"][
                                        "toolUseId"
                                    ]
                                    debug_print(
                                        f"Tool use detected: {self.toolName}, ID: {self.toolUseId}"
                                    )
                                elif (
                                    "contentEnd" in json_data["event"]
                                    and json_data["event"]
                                    .get("contentEnd", {})
                                    .get("type")
                                    == "TOOL"
                                ):
                                    debug_print(
                                        "Processing tool use and sending result"
                                    )
                                    # Start asynchronous tool processing - non-blocking
                                    self.handle_tool_request(
                                        self.toolName,
                                        self.toolUseContent,
                                        self.toolUseId,
                                    )
                                    debug_print("Processing tool use asynchronously")
                                elif "contentEnd" in json_data["event"]:
                                    debug_print("Content end")
                                elif "completionEnd" in json_data["event"]:
                                    # Handle end of conversation, no more response will be generated
                                    debug_print("End of response sequence")
                                elif "usageEvent" in json_data["event"]:
                                    debug_print(f"UsageEvent: {json_data['event']}")
                            # Put the response in the output queue for other components
                            await self.output_queue.put(json_data)
                        except json.JSONDecodeError:
                            await self.output_queue.put({"raw_data": response_data})
                except StopAsyncIteration:
                    # Stream has ended
                    break
                except Exception as e:
                    # Handle ValidationException properly
                    if "ValidationException" in str(e):
                        error_message = str(e)
                        print(f"Validation error: {error_message}")
                    else:
                        print(f"Error receiving response: {e}")
                    break

        except Exception as e:
            print(f"Response processing error: {e}")
        finally:
            self.is_active = False

    def handle_tool_request(self, tool_name, tool_content, tool_use_id):
        """Handle a tool request asynchronously"""
        # Create a unique content name for this tool response
        tool_content_name = str(uuid.uuid4())

        # Create an asynchronous task for the tool execution
        task = asyncio.create_task(
            self._execute_tool_and_send_result(
                tool_name, tool_content, tool_use_id, tool_content_name
            )
        )

        # Store the task
        self.pending_tool_tasks[tool_content_name] = task

        # Add error handling
        task.add_done_callback(
            lambda t: self._handle_tool_task_completion(t, tool_content_name)
        )

    def _handle_tool_task_completion(self, task, content_name):
        """Handle the completion of a tool task"""
        # Remove task from pending tasks
        if content_name in self.pending_tool_tasks:
            del self.pending_tool_tasks[content_name]

        # Handle any exceptions
        if task.done() and not task.cancelled():
            exception = task.exception()
            if exception:
                debug_print(f"Tool task failed: {str(exception)}")

    async def _execute_tool_and_send_result(
        self, tool_name, tool_content, tool_use_id, content_name
    ):
        """Execute a tool and send the result"""
        try:
            debug_print(f"Starting tool execution: {tool_name}")

            # Process the tool - this doesn't block the event loop
            tool_result = await self.tool_processor.process_tool_async(
                tool_name, tool_content
            )

            # Send the result sequence
            await self.send_tool_start_event(content_name, tool_use_id)
            await self.send_tool_result_event(content_name, tool_result)
            await self.send_tool_content_end_event(content_name)

            debug_print(f"Tool execution complete: {tool_name}")
        except Exception as e:
            debug_print(f"Error executing tool {tool_name}: {str(e)}")
            # Try to send an error response if possible
            try:
                error_result = {"error": f"Tool execution failed: {str(e)}"}

                await self.send_tool_start_event(content_name, tool_use_id)
                await self.send_tool_result_event(content_name, error_result)
                await self.send_tool_content_end_event(content_name)
            except Exception as send_error:
                debug_print(f"Failed to send error response: {str(send_error)}")

    async def close(self):
        """Close the stream properly."""
        if not self.is_active:
            return

        # Cancel any pending tool tasks
        for task in self.pending_tool_tasks.values():
            task.cancel()

        if self.response_task and not self.response_task.done():
            self.response_task.cancel()

        await self.send_audio_content_end_event()
        await self.send_prompt_end_event()
        await self.send_session_end_event()

        if self.stream_response:
            await self.stream_response.input_stream.close()


class AudioStreamer:
    """Handles continuous microphone input and audio output using separate streams."""

    def __init__(self, stream_manager):
        self.stream_manager = stream_manager
        self.is_streaming = False
        self.loop = asyncio.get_event_loop()

        # Initialize PyAudio
        debug_print("AudioStreamer Initializing PyAudio...")
        self.p = time_it("AudioStreamerInitPyAudio", pyaudio.PyAudio)
        debug_print("AudioStreamer PyAudio initialized")

        # Initialize separate streams for input and output
        # Input stream with callback for microphone
        debug_print("Opening input audio stream...")
        self.input_stream = time_it(
            "AudioStreamerOpenAudio",
            lambda: self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=INPUT_SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
                stream_callback=self.input_callback,
            ),
        )
        debug_print("input audio stream opened")

        # Output stream for direct writing (no callback)
        debug_print("Opening output audio stream...")
        self.output_stream = time_it(
            "AudioStreamerOpenAudio",
            lambda: self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=OUTPUT_SAMPLE_RATE,
                output=True,
                frames_per_buffer=CHUNK_SIZE,
            ),
        )

        debug_print("output audio stream opened")

    def input_callback(self, in_data, frame_count, time_info, status):
        """Callback function that schedules audio processing in the asyncio event loop"""
        if self.is_streaming and in_data:
            # Schedule the task in the event loop
            asyncio.run_coroutine_threadsafe(
                self.process_input_audio(in_data), self.loop
            )
        return (None, pyaudio.paContinue)

    async def process_input_audio(self, audio_data):
        """Process a single audio chunk directly"""
        try:
            # Send audio to Bedrock immediately
            self.stream_manager.add_audio_chunk(audio_data)
        except Exception as e:
            if self.is_streaming:
                print(f"Error processing input audio: {e}")

    async def play_output_audio(self):
        """Play audio responses from Nova Sonic"""
        while self.is_streaming:
            try:
                # Check for barge-in flag
                if self.stream_manager.barge_in:
                    # Clear the audio queue
                    while not self.stream_manager.audio_output_queue.empty():
                        try:
                            self.stream_manager.audio_output_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    self.stream_manager.barge_in = False
                    # Small sleep after clearing
                    await asyncio.sleep(0.05)
                    continue

                # Get audio data from the stream manager's queue
                audio_data = await asyncio.wait_for(
                    self.stream_manager.audio_output_queue.get(), timeout=0.1
                )

                if audio_data and self.is_streaming:
                    # Write directly to the output stream in smaller chunks
                    chunk_size = CHUNK_SIZE  # Use the same chunk size as the stream

                    # Write the audio data in chunks to avoid blocking too long
                    for i in range(0, len(audio_data), chunk_size):
                        if not self.is_streaming:
                            break

                        end = min(i + chunk_size, len(audio_data))
                        chunk = audio_data[i:end]

                        # Create a new function that captures the chunk by value
                        def write_chunk(data):
                            return self.output_stream.write(data)

                        # Pass the chunk to the function
                        await asyncio.get_event_loop().run_in_executor(
                            None, write_chunk, chunk
                        )

                        # Brief yield to allow other tasks to run
                        await asyncio.sleep(0.001)

            except asyncio.TimeoutError:
                # No data available within timeout, just continue
                continue
            except Exception as e:
                if self.is_streaming:
                    print(f"Error playing output audio: {str(e)}")
                    import traceback

                    traceback.print_exc()
                await asyncio.sleep(0.05)

    async def start_streaming(self):
        """Start streaming audio."""
        if self.is_streaming:
            return

        print("Starting audio streaming. Speak into your microphone...")
        print("Press Enter to stop streaming...")

        # Send audio content start event
        await time_it_async(
            "send_audio_content_start_event",
            lambda: self.stream_manager.send_audio_content_start_event(),
        )

        self.is_streaming = True

        # Start the input stream if not already started
        if not self.input_stream.is_active():
            self.input_stream.start_stream()

        # Start processing tasks
        # self.input_task = asyncio.create_task(self.process_input_audio())
        self.output_task = asyncio.create_task(self.play_output_audio())

        # Wait for user to press Enter to stop
        await asyncio.get_event_loop().run_in_executor(None, input)

        # Once input() returns, stop streaming
        await self.stop_streaming()

    async def stop_streaming(self):
        """Stop streaming audio."""
        if not self.is_streaming:
            return

        self.is_streaming = False

        # Cancel the tasks
        tasks = []
        if hasattr(self, "input_task") and not self.input_task.done():
            tasks.append(self.input_task)
        if hasattr(self, "output_task") and not self.output_task.done():
            tasks.append(self.output_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Stop and close the streams
        if self.input_stream:
            if self.input_stream.is_active():
                self.input_stream.stop_stream()
            self.input_stream.close()
        if self.output_stream:
            if self.output_stream.is_active():
                self.output_stream.stop_stream()
            self.output_stream.close()
        if self.p:
            self.p.terminate()

        await self.stream_manager.close()


async def main(debug=False):
    """Main function to run the application."""
    global DEBUG
    DEBUG = debug

    # Create stream manager
    stream_manager = BedrockStreamManager(
        model_id="amazon.nova-sonic-v1:0", region="us-east-1"
    )

    # Create audio streamer
    audio_streamer = AudioStreamer(stream_manager)

    # Initialize the stream
    await time_it_async("initialize_stream", stream_manager.initialize_stream)

    try:
        # This will run until the user presses Enter
        await audio_streamer.start_streaming()

    except KeyboardInterrupt:
        print("Interrupted by user")
    finally:
        # Clean up
        await audio_streamer.stop_streaming()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Nova Sonic Python Streaming")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()
    # Set your AWS credentials here or use environment variables
    os.environ["AWS_ACCESS_KEY_ID"] = ""
    os.environ["AWS_SECRET_ACCESS_KEY"] = ""
    os.environ["AWS_DEFAULT_REGION"] = ""

    # Run the main function
    try:
        asyncio.run(main(debug=args.debug))
    except Exception as e:
        print(f"Application error: {e}")
        if args.debug:
            import traceback

            traceback.print_exc()
