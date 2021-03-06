"""Abstract class for writing chat clients."""

import aiohttp
import asyncio
import json
import logging
import random
import time
import datetime
import os

from hangups import (javascript, parsers, exceptions, http_utils, channel,
                     event, hangouts_pb2, pblite, __version__)

logger = logging.getLogger(__name__)
ORIGIN_URL = 'https://talkgadget.google.com'
IMAGE_UPLOAD_URL = 'http://docs.google.com/upload/photos/resumable'
# Timeout to send for setactiveclient requests:
ACTIVE_TIMEOUT_SECS = 120
# Minimum timeout between subsequent setactiveclient requests:
SETACTIVECLIENT_LIMIT_SECS = 60


class Client(object):
    """Instant messaging client for Hangouts.

    Maintains a connections to the servers, emits events, and accepts commands.
    """

    def __init__(self, cookies):
        """Create new client.

        cookies is a dictionary of authentication cookies.
        """

        # Event fired when the client connects for the first time with
        # arguments ().
        self.on_connect = event.Event('Client.on_connect')
        # Event fired when the client reconnects after being disconnected with
        # arguments ().
        self.on_reconnect = event.Event('Client.on_reconnect')
        # Event fired when the client is disconnected with arguments ().
        self.on_disconnect = event.Event('Client.on_disconnect')
        # Event fired when a StateUpdate arrives with arguments (state_update).
        self.on_state_update = event.Event('Client.on_state_update')

        self._cookies = cookies
        proxy = os.environ.get('HTTP_PROXY')
        if proxy:
            self._connector = aiohttp.ProxyConnector(proxy)
        else:
            self._connector = aiohttp.TCPConnector()

        self._channel = channel.Channel(self._cookies, self._connector)
        # Future for Channel.listen
        self._listen_future = None

        self._request_header = hangouts_pb2.RequestHeader(
            # Ignore most of the RequestHeader fields since they aren't
            # required.
            client_version=hangouts_pb2.ClientVersion(
                major_version='hangups-{}'.format(__version__),
            ),
            language_code='en',
        )

        # String identifying this client (populated later):
        self._client_id = None

        # String email address for this account (populated later):
        self._email = None

        # Active client management parameters:
        # Time in seconds that the client as last set as active:
        self._last_active_secs = 0.0
        # ActiveClientState enum int value or None:
        self._active_client_state = None

    ##########################################################################
    # Public methods
    ##########################################################################

    @asyncio.coroutine
    def connect(self):
        """Establish a connection to the chat server.

        Returns when an error has occurred, or Client.disconnect has been
        called.
        """
        # Forward the Channel events to the Client events.
        self._channel.on_connect.add_observer(self.on_connect.fire)
        self._channel.on_reconnect.add_observer(self.on_reconnect.fire)
        self._channel.on_disconnect.add_observer(self.on_disconnect.fire)
        self._channel.on_receive_array.add_observer(self._on_receive_array)

        # Listen for StateUpdate messages from the Channel until it
        # disconnects.
        self._listen_future = asyncio.async(self._channel.listen())
        try:
            yield from self._listen_future
        except asyncio.CancelledError:
            pass
        self._connector.close()
        logger.info('Client.connect returning because Channel.listen returned')

    @asyncio.coroutine
    def disconnect(self):
        """Gracefully disconnect from the server.

        When disconnection is complete, Client.connect will return.
        """
        logger.info('Disconnecting gracefully...')
        self._listen_future.cancel()
        try:
            yield from self._listen_future
        except asyncio.CancelledError:
            pass
        logger.info('Disconnected gracefully')

    @asyncio.coroutine
    def set_active(self):
        """Set this client as active.

        While a client is active, no other clients will raise notifications.
        Call this method whenever there is an indication the user is
        interacting with this client. This method may be called very
        frequently, and it will only make a request when necessary.
        """
        is_active = (self._active_client_state ==
                     hangouts_pb2.ACTIVE_CLIENT_STATE_IS_ACTIVE)
        timed_out = (time.time() - self._last_active_secs >
                     SETACTIVECLIENT_LIMIT_SECS)
        if not is_active or timed_out:
            # Update these immediately so if the function is called again
            # before the API request finishes, we don't start extra requests.
            self._active_client_state = (
                hangouts_pb2.ACTIVE_CLIENT_STATE_IS_ACTIVE
            )
            self._last_active_secs = time.time()

            # The first time this is called, we need to retrieve the user's
            # email address.
            if self._email is None:
                try:
                    get_self_info_response = yield from self.getselfinfo()
                except exceptions.NetworkError as e:
                    logger.warning('Failed to find email address: {}'
                                   .format(e))
                    return
                self._email = (
                    get_self_info_response.self_entity.properties.email[0]
                )

            # If the client_id hasn't been received yet, we can't set the
            # active client.
            if self._client_id is None:
                logger.info(
                    'Cannot set active client until client_id is received'
                )
                return

            try:
                yield from self.setactiveclient(True, ACTIVE_TIMEOUT_SECS)
            except exceptions.NetworkError as e:
                logger.warning('Failed to set active client: {}'.format(e))
            else:
                logger.info('Set active client for {} seconds'
                            .format(ACTIVE_TIMEOUT_SECS))

    ##########################################################################
    # Private methods
    ##########################################################################

    def _get_cookie(self, name):
        """Return a cookie for raise error if that cookie was not provided."""
        try:
            return self._cookies[name]
        except KeyError:
            raise KeyError("Cookie '{}' is required".format(name))

    @asyncio.coroutine
    def _on_receive_array(self, array):
        """Parse channel array and call the appropriate events."""
        if array[0] == 'noop':
            pass  # This is just a keep-alive, ignore it.
        else:
            wrapper = json.loads(array[0]['p'])
            # Wrapper appears to be a Protocol Buffer message, but encoded via
            # field numbers as dictionary keys. Since we don't have a parser
            # for that, parse it ad-hoc here.
            if '3' in wrapper:
                # This is a new client_id.
                self._client_id = wrapper['3']['2']
                logger.info('Received new client_id: %r', self._client_id)
                # Once client_id is received, the channel is ready to have
                # services added.
                yield from self._add_channel_services()
            if '2' in wrapper:
                pblite_message = json.loads(wrapper['2']['2'])
                if pblite_message[0] == 'cbu':
                    # This is a (Client)BatchUpdate containing StateUpdate
                    # messages.
                    batch_update = hangouts_pb2.BatchUpdate()
                    pblite.decode(batch_update, pblite_message,
                                  ignore_first_item=True)
                    for state_update in batch_update.state_update:
                        logger.debug('Received StateUpdate:\n%s', state_update)
                        header = state_update.state_update_header
                        self._active_client_state = header.active_client_state
                        yield from self.on_state_update.fire(state_update)
                else:
                    logger.info('Ignoring message: %r', pblite_message[0])

    @asyncio.coroutine
    def _add_channel_services(self):
        """Add services to the channel.

        The services we add to the channel determine what kind of data we will
        receive on it. The "babel" service includes what we need for Hangouts.
        If this fails for some reason, hangups will never receive any events.

        This needs to be re-called whenever we open a new channel (when there's
        a new SID and client_id.
        """
        logger.info('Adding channel services...')
        # Based on what Hangouts for Chrome does over 2 requests, this is
        # trimmed down to 1 request that includes the bare minimum to make
        # things work.
        map_list = [dict(p=json.dumps({"3": {"1": {"1": "babel"}}}))]
        yield from self._channel.send_maps(map_list)
        logger.info('Channel services added')

    @asyncio.coroutine
    def _pb_request(self, endpoint, request_pb, response_pb):
        """Send a Protocol Buffer formatted chat API request.

        Args:
            endpoint (str): The chat API endpoint to use.
            request_pb: The request body as a Protocol Buffer message.
            response_pb: The response body as a Protocol Buffer message.

        Raises:
            NetworkError: If the request fails.
        """
        logger.debug('Sending Protocol Buffer request %s:\n%s', endpoint,
                     request_pb)
        res = yield from self._base_request(
            'https://clients6.google.com/chat/v1/{}'.format(endpoint),
            'application/json+protobuf',  # The request body is pblite.
            'protojson',  # The response should be pblite.
            json.dumps(pblite.encode(request_pb))
        )
        pblite.decode(response_pb, javascript.loads(res.body.decode()),
                      ignore_first_item=True)
        logger.debug('Received Protocol Buffer response:\n%s', response_pb)
        status = response_pb.response_header.status
        if status != hangouts_pb2.RESPONSE_STATUS_OK:
            description = response_pb.response_header.error_description
            raise exceptions.NetworkError(
                'Request failed with status {}: \'{}\''
                .format(status, description)
            )

    @asyncio.coroutine
    def _base_request(self, url, content_type, response_type, data):
        """Send a generic authenticated POST request.

        Args:
            url (str): URL of request.
            content_type (str): Request content type.
            response_type (str): The desired response format. Valid options
                are: 'json' (JSON), 'protojson' (pblite), and 'proto' (binary
                Protocol Buffer). 'proto' requires manually setting an extra
                header 'X-Goog-Encode-Response-If-Executable: base64'.
            data (str): Request body data.

        Returns:
            FetchResponse: Response containing HTTP code, cookies, and body.

        Raises:
            NetworkError: If the request fails.
        """
        sapisid_cookie = self._get_cookie('SAPISID')
        headers = channel.get_authorization_headers(sapisid_cookie)
        headers['content-type'] = content_type
        required_cookies = ['SAPISID', 'HSID', 'SSID', 'APISID', 'SID']
        cookies = {cookie: self._get_cookie(cookie)
                   for cookie in required_cookies}
        params = {
            # "alternative representation type" (desired response format).
            'alt': response_type,
        }
        res = yield from http_utils.fetch(
            'post', url, headers=headers, cookies=cookies, params=params,
            data=data, connector=self._connector
        )
        return res

    def _get_request_header_pb(self):
        """Return populated RequestHeader message."""
        # resource is allowed to be null if it's not available yet (the Chrome
        # client does this for the first getentitybyid call)
        if self._client_id is not None:
            self._request_header.client_identifier.resource = self._client_id
        return self._request_header

    def get_client_generated_id(self):
        """Return ID for client_generated_id fields."""
        return random.randint(0, 2**32)

    ###########################################################################
    # Raw API request methods
    ###########################################################################

    @asyncio.coroutine
    def syncallnewevents(self, timestamp):
        """List all events occurring at or after timestamp.

        This method requests protojson rather than json so we have one chat
        message parser rather than two.

        timestamp: datetime.datetime instance specifying the time after
        which to return all events occurring in.

        Raises hangups.NetworkError if the request fails.

        Returns SyncAllNewEventsResponse.
        """
        request = hangouts_pb2.SyncAllNewEventsRequest(
            request_header=self._get_request_header_pb(),
            last_sync_timestamp=parsers.to_timestamp(timestamp),
            max_response_size_bytes=1048576,
        )
        response = hangouts_pb2.SyncAllNewEventsResponse()
        yield from self._pb_request('conversations/syncallnewevents', request,
                                    response)
        return response

    @asyncio.coroutine
    def sendchatmessage(
            self, conversation_id, segments, image_id=None,
            otr_status=hangouts_pb2.OFF_THE_RECORD_STATUS_ON_THE_RECORD,
            delivery_medium=None):
        """Send a chat message to a conversation.

        conversation_id must be a valid conversation ID. segments must be a
        list of message segments to send, in pblite format.

        otr_status determines whether the message will be saved in the server's
        chat history. Note that the OTR status of the conversation is
        irrelevant, clients may send messages with whatever OTR status they
        like.

        image_id is an option ID of an image retrieved from
        Client.upload_image. If provided, the image will be attached to the
        message.

        Raises hangups.NetworkError if the request fails.
        """
        segments_pb = []
        for segment_pblite in segments:
            segment_pb = hangouts_pb2.Segment()
            pblite.decode(segment_pb, segment_pblite)
            segments_pb.append(segment_pb)
        if delivery_medium is None:
            delivery_medium = hangouts_pb2.DeliveryMedium(
                medium_type=hangouts_pb2.DELIVERY_MEDIUM_BABEL,
            )

        request = hangouts_pb2.SendChatMessageRequest(
            request_header=self._get_request_header_pb(),
            message_content=hangouts_pb2.MessageContent(
                segment=segments_pb,
            ),
            event_request_header=hangouts_pb2.EventRequestHeader(
                conversation_id=hangouts_pb2.ConversationId(
                    id=conversation_id,
                ),
                client_generated_id=self.get_client_generated_id(),
                expected_otr=otr_status,
                delivery_medium=delivery_medium,
                event_type=hangouts_pb2.EVENT_TYPE_REGULAR_CHAT_MESSAGE,
            ),
        )

        if image_id is not None:
            request.existing_media = hangouts_pb2.ExistingMedia(
                photo=hangouts_pb2.Photo(photo_id=image_id)
            )

        response = hangouts_pb2.SendChatMessageResponse()
        yield from self._pb_request('conversations/sendchatmessage', request,
                                    response)
        return response

    @asyncio.coroutine
    def setactiveclient(self, is_active, timeout_secs):
        """Set the active client.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.SetActiveClientRequest(
            request_header=self._get_request_header_pb(),
            is_active=is_active,
            full_jid="{}/{}".format(self._email, self._client_id),
            timeout_secs=timeout_secs,
        )
        response = hangouts_pb2.SetActiveClientResponse()
        yield from self._pb_request('clients/setactiveclient', request,
                                    response)
        return response

    @asyncio.coroutine
    def updatewatermark(self, conv_id, read_timestamp):
        """Update the watermark (read timestamp) for a conversation.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.UpdateWatermarkRequest(
            request_header=self._get_request_header_pb(),
            conversation_id=hangouts_pb2.ConversationId(id=conv_id),
            last_read_timestamp=parsers.to_timestamp(read_timestamp),
        )
        response = hangouts_pb2.UpdateWatermarkResponse()
        yield from self._pb_request('conversations/updatewatermark', request,
                                    response)
        return response

    @asyncio.coroutine
    def getentitybyid(self, gaia_id_list):
        """Return information about a list of contacts.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.GetEntityByIdRequest(
            request_header=self._get_request_header_pb(),
            batch_lookup_spec=[hangouts_pb2.EntityLookupSpec(gaia_id=gaia_id)
                               for gaia_id in gaia_id_list],
        )
        response = hangouts_pb2.GetEntityByIdResponse()
        yield from self._pb_request('contacts/getentitybyid', request,
                                    response)
        return response

    @asyncio.coroutine
    def renameconversation(
            self, conversation_id, name,
            otr_status=hangouts_pb2.OFF_THE_RECORD_STATUS_ON_THE_RECORD):
        """Rename a conversation.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.RenameConversationRequest(
            request_header=self._get_request_header_pb(),
            new_name=name,
            event_request_header=hangouts_pb2.EventRequestHeader(
                conversation_id=hangouts_pb2.ConversationId(
                    id=conversation_id,
                ),
                client_generated_id=self.get_client_generated_id(),
                expected_otr=otr_status,
            ),
        )
        response = hangouts_pb2.RenameConversationResponse()
        yield from self._pb_request('conversations/renameconversation',
                                    request, response)
        return response

    @asyncio.coroutine
    def getconversation(self, conversation_id, event_timestamp, max_events=50):
        """Return conversation events.

        This is mainly used for retrieving conversation scrollback. Events
        occurring before event_timestamp are returned, in order from oldest to
        newest.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.GetConversationRequest(
            request_header=self._get_request_header_pb(),
            conversation_spec=hangouts_pb2.ConversationSpec(
                conversation_id=hangouts_pb2.ConversationId(id=conversation_id)
            ),
            include_event=True,
            max_events_per_conversation=max_events,
            event_continuation_token=hangouts_pb2.EventContinuationToken(
                event_timestamp=parsers.to_timestamp(event_timestamp)
            ),
        )
        response = hangouts_pb2.GetConversationResponse()
        yield from self._pb_request('conversations/getconversation', request,
                                    response)
        return response

    @asyncio.coroutine
    def upload_image(self, image_file, filename=None):
        """Upload an image that can be later attached to a chat message.

        image_file is a file-like object containing an image.

        The name of the uploaded file may be changed by specifying the filename
        argument.

        Raises hangups.NetworkError if the request fails.

        Returns ID of uploaded image.
        """
        image_filename = (filename if filename
                          else os.path.basename(image_file.name))
        image_data = image_file.read()

        # Create image and request upload URL
        res1 = yield from self._base_request(
            IMAGE_UPLOAD_URL,
            'application/x-www-form-urlencoded;charset=UTF-8',
            'json',
            json.dumps({
                "protocolVersion": "0.8",
                "createSessionRequest": {
                    "fields": [{
                        "external": {
                            "name": "file",
                            "filename": image_filename,
                            "put": {},
                            "size": len(image_data),
                        }
                    }]
                }
            }))
        upload_url = (json.loads(res1.body.decode())['sessionStatus']
                      ['externalFieldTransfers'][0]['putInfo']['url'])

        # Upload image data and get image ID
        res2 = yield from self._base_request(
            upload_url, 'application/octet-stream', 'json', image_data
        )
        return (json.loads(res2.body.decode())['sessionStatus']
                ['additionalInfo']
                ['uploader_service.GoogleRupioAdditionalInfo']
                ['completionInfo']['customerSpecificInfo']['photoid'])

    ###########################################################################
    # UNUSED raw API request methods (by hangups itself) for reference
    ###########################################################################

    @asyncio.coroutine
    def removeuser(
            self, conversation_id,
            otr_status=hangouts_pb2.OFF_THE_RECORD_STATUS_ON_THE_RECORD):
        """Leave group conversation.

        conversation_id must be a valid conversation ID.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.RemoveUserRequest(
            request_header=self._get_request_header_pb(),
            event_request_header=hangouts_pb2.EventRequestHeader(
                conversation_id=hangouts_pb2.ConversationId(
                    id=conversation_id,
                ),
                client_generated_id=self.get_client_generated_id(),
                expected_otr=otr_status,
            ),
        )
        response = hangouts_pb2.RemoveUserResponse()
        yield from self._pb_request('conversations/removeuser', request,
                                    response)
        return response

    @asyncio.coroutine
    def deleteconversation(self, conversation_id):
        """Delete one-to-one conversation.

        One-to-one conversations are "sticky"; they can't actually be deleted.
        This API clears the event history of the specified conversation up to
        delete_upper_bound_timestamp, hiding it if no events remain.

        conversation_id must be a valid conversation ID.

        Raises hangups.NetworkError if the request fails.
        """
        timestamp = parsers.to_timestamp(
            datetime.datetime.now(tz=datetime.timezone.utc)
        )
        request = hangouts_pb2.DeleteConversationRequest(
            request_header=self._get_request_header_pb(),
            conversation_id=hangouts_pb2.ConversationId(id=conversation_id),
            delete_upper_bound_timestamp=timestamp
        )
        response = hangouts_pb2.DeleteConversationResponse()
        yield from self._pb_request('conversations/deleteconversation',
                                    request, response)
        return response

    @asyncio.coroutine
    def settyping(self, conversation_id,
                  typing=hangouts_pb2.TYPING_TYPE_STARTED):
        """Send typing notification.

        conversation_id must be a valid conversation ID.
        typing must be a hangups.TypingType Enum.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.SetTypingRequest(
            request_header=self._get_request_header_pb(),
            conversation_id=hangouts_pb2.ConversationId(id=conversation_id),
            type=typing,
        )
        response = hangouts_pb2.SetTypingResponse()
        yield from self._pb_request('conversations/settyping', request,
                                    response)
        return response

    @asyncio.coroutine
    def getselfinfo(self):
        """Return information about your account.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.GetSelfInfoRequest(
            request_header=self._get_request_header_pb(),
        )
        response = hangouts_pb2.GetSelfInfoResponse()
        yield from self._pb_request('contacts/getselfinfo', request, response)
        return response

    @asyncio.coroutine
    def setfocus(self, conversation_id):
        """Set focus to a conversation.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.SetFocusRequest(
            request_header=self._get_request_header_pb(),
            conversation_id=hangouts_pb2.ConversationId(id=conversation_id),
            type=hangouts_pb2.FOCUS_TYPE_FOCUSED,
            timeout_secs=20,
        )
        response = hangouts_pb2.SetFocusResponse()
        yield from self._pb_request('conversations/setfocus', request,
                                    response)
        return response

    @asyncio.coroutine
    def searchentities(self, search_string, max_results):
        """Search for people.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.SearchEntitiesRequest(
            request_header=self._get_request_header_pb(),
            query=search_string,
            max_count=max_results,
        )
        response = hangouts_pb2.SearchEntitiesResponse()
        yield from self._pb_request('contacts/searchentities', request,
                                    response)
        return response

    @asyncio.coroutine
    def setpresence(self, online, mood=None):
        """Set the presence or mood of this client.

        Raises hangups.NetworkError if the request fails.
        """
        type_ = (hangouts_pb2.CLIENT_PRESENCE_STATE_DESKTOP_ACTIVE if online
                 else hangouts_pb2.CLIENT_PRESENCE_STATE_DESKTOP_IDLE)
        request = hangouts_pb2.SetPresenceRequest(
            request_header=self._get_request_header_pb(),
            presence_state_setting=hangouts_pb2.PresenceStateSetting(
                timeout_secs=720,
                type=type_,
            ),
        )
        if mood is not None:
            segment = (
                request.mood_setting.mood_message.mood_content.segment.add()
            )
            segment.type = hangouts_pb2.SEGMENT_TYPE_TEXT
            segment.text = mood
        response = hangouts_pb2.SetPresenceResponse()
        yield from self._pb_request('presence/setpresence', request, response)
        return response

    @asyncio.coroutine
    def querypresence(self, gaia_id):
        """Check someone's presence status.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.QueryPresenceRequest(
            request_header=self._get_request_header_pb(),
            participant_id=[hangouts_pb2.ParticipantId(gaia_id=gaia_id)],
            field_mask=[hangouts_pb2.FIELD_MASK_REACHABLE,
                        hangouts_pb2.FIELD_MASK_AVAILABLE,
                        hangouts_pb2.FIELD_MASK_DEVICE],
        )
        response = hangouts_pb2.QueryPresenceResponse()
        yield from self._pb_request('presence/querypresence', request,
                                    response)
        return response

    @asyncio.coroutine
    def syncrecentconversations(self, max_conversations=100,
                                max_events_per_conversation=1):
        """List the contents of recent conversations, including messages.

        Similar to syncallnewevents, but returns a limited number of
        conversations rather than all conversations in a given date range.

        Can be used to retrieve archived conversations.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.SyncRecentConversationsRequest(
            request_header=self._get_request_header_pb(),
            max_conversations=max_conversations,
            max_events_per_conversation=max_events_per_conversation,
            sync_filter=[hangouts_pb2.SYNC_FILTER_INBOX],
        )
        response = hangouts_pb2.SyncRecentConversationsResponse()
        yield from self._pb_request('conversations/syncrecentconversations',
                                    request, response)
        return response

    @asyncio.coroutine
    def setconversationnotificationlevel(self, conversation_id, level):
        """Set the notification level of a conversation.

        Pass hangouts_pb2.NOTIFICATION_LEVEL_QUIET to disable notifications, or
        hangouts_pb2.NOTIFICATION_LEVEL_RING to enable them.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.SetConversationNotificationLevelRequest(
            request_header=self._get_request_header_pb(),
            conversation_id=hangouts_pb2.ConversationId(id=conversation_id),
            level=level,
        )
        response = hangouts_pb2.SetConversationNotificationLevelResponse()
        yield from self._pb_request(
            'conversations/setconversationnotificationlevel', request, response
        )
        return response

    @asyncio.coroutine
    def easteregg(self, conversation_id, easteregg):
        """Send an easteregg to a conversation.

        easteregg may not be empty.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.EasterEggRequest(
            request_header=self._get_request_header_pb(),
            conversation_id=hangouts_pb2.ConversationId(id=conversation_id),
            easter_egg=hangouts_pb2.EasterEgg(message=easteregg),
        )
        response = hangouts_pb2.EasterEggResponse()
        yield from self._pb_request('conversations/easteregg', request,
                                    response)
        return response

    @asyncio.coroutine
    def createconversation(self, chat_id_list, force_group=False):
        """Create new one-to-one or group conversation.

        chat_id_list is list of other users to invite to the conversation.

        Raises hangups.NetworkError if the request fails.
        """
        is_group = len(chat_id_list) > 1 or force_group
        request = hangouts_pb2.CreateConversationRequest(
            request_header=self._get_request_header_pb(),
            type=(hangouts_pb2.CONVERSATION_TYPE_GROUP if is_group else
                  hangouts_pb2.CONVERSATION_TYPE_ONE_TO_ONE),
            client_generated_id=self.get_client_generated_id(),
            invitee_id=[hangouts_pb2.InviteeID(gaia_id=chat_id)
                        for chat_id in chat_id_list],
        )
        response = hangouts_pb2.CreateConversationResponse()
        yield from self._pb_request('conversations/createconversation',
                                    request, response)
        return response

    @asyncio.coroutine
    def adduser(self, conversation_id, chat_id_list,
                otr_status=hangouts_pb2.OFF_THE_RECORD_STATUS_ON_THE_RECORD):
        """Add users to an existing group conversation.

        conversation_id must be a valid conversation ID.
        chat_id_list is list of users which should be invited to conversation.

        Raises hangups.NetworkError if the request fails.
        """
        request = hangouts_pb2.AddUserRequest(
            request_header=self._get_request_header_pb(),
            invitee_id=[hangouts_pb2.InviteeID(gaia_id=chat_id)
                        for chat_id in chat_id_list],
            event_request_header=hangouts_pb2.EventRequestHeader(
                conversation_id=hangouts_pb2.ConversationId(
                    id=conversation_id,
                ),
                client_generated_id=self.get_client_generated_id(),
                expected_otr=otr_status,
            ),
        )
        response = hangouts_pb2.AddUserResponse()
        yield from self._pb_request('conversations/adduser', request, response)
        return response
