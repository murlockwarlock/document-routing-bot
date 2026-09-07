from __future__ import annotations

from abc import ABC, abstractmethod

from multichannel_gateway.core.models import AttachmentRef, DownloadedAttachment, InboundEnvelope


class TransportAdapter(ABC):
    name: str

    @abstractmethod
    async def download_attachment(self, envelope: InboundEnvelope, attachment: AttachmentRef) -> DownloadedAttachment:
        raise NotImplementedError
