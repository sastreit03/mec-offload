"""JSON envelope inside a versioned protobuf message, independent of old OAI protobufs."""

from .wire import dumps, loads

SERVICE = "mec.awareness.v1.Awareness"


class Client:
    def __init__(self, target):
        self.target = target
        self.channel = None

    async def connect(self):
        import grpc
        from .generated import mec_edgeric_pb2 as pb

        self.channel = grpc.aio.insecure_channel(
            self.target, options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)]
        )
        self.pb = pb
        self.exchange = self.channel.unary_unary(
            "/" + SERVICE + "/Exchange",
            request_serializer=pb.Envelope.SerializeToString,
            response_deserializer=pb.Envelope.FromString,
        )
        self.reports = self.channel.unary_stream(
            "/" + SERVICE + "/Reports",
            request_serializer=pb.Envelope.SerializeToString,
            response_deserializer=pb.Envelope.FromString,
        )

    # Awareness updates use bounded unary calls; telemetry uses a separate stream.
    async def call(self, value, timeout=0.25):
        if self.channel is None:
            await self.connect()
        result = await self.exchange(
            self.pb.Envelope(schema_version=1, json_utf8=dumps(value).encode()),
            timeout=timeout,
        )
        if result.schema_version != 1:
            raise ValueError("RPC schema mismatch")
        return loads(result.json_utf8)

    async def stream(self, value):
        if self.channel is None:
            await self.connect()
        async for result in self.reports(
            self.pb.Envelope(schema_version=1, json_utf8=dumps(value).encode())
        ):
            if result.schema_version != 1:
                raise ValueError("RPC schema mismatch")
            yield loads(result.json_utf8)

    async def close(self):
        if self.channel:
            await self.channel.close()


# Bind JSON-envelope handlers without depending on the legacy OAI protobuf schema.
def add_service(server, exchange, reports):
    import grpc
    from .generated import mec_edgeric_pb2 as pb

    async def unary(request, context):
        if request.schema_version != 1:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "schema mismatch")
        try:
            result = await exchange(loads(request.json_utf8))
        except (ValueError, KeyError, TypeError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return pb.Envelope(schema_version=1, json_utf8=dumps(result).encode())

    async def stream(request, context):
        if request.schema_version != 1:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "schema mismatch")
        async for result in reports(loads(request.json_utf8)):
            yield pb.Envelope(schema_version=1, json_utf8=dumps(result).encode())

    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                SERVICE,
                {
                    "Exchange": grpc.unary_unary_rpc_method_handler(
                        unary,
                        request_deserializer=pb.Envelope.FromString,
                        response_serializer=pb.Envelope.SerializeToString,
                    ),
                    "Reports": grpc.unary_stream_rpc_method_handler(
                        stream,
                        request_deserializer=pb.Envelope.FromString,
                        response_serializer=pb.Envelope.SerializeToString,
                    ),
                },
            ),
        )
    )
