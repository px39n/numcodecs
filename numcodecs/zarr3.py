from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cached_property
import math
from typing_extensions import Self
from warnings import warn

import numpy as np
import numcodecs

from zarr.abc.codec import ArrayArrayCodec, BytesBytesCodec
from zarr.buffer import NDBuffer, Buffer, as_numpy_array_wrapper
from zarr.common import (
    JSON,
    ArraySpec,
    parse_named_configuration,
    product,
    to_thread,
)
from zarr.metadata import ArrayMetadata


CODEC_PREFIX = "https://zarr.dev/numcodecs/"


def parse_codec_configuration(
    data: dict[str, JSON], expected_name_prefix: str
) -> dict[str, JSON]:
    parsed_name, parsed_configuration = parse_named_configuration(data)
    if not parsed_name.startswith(expected_name_prefix):
        raise ValueError(
            f"Expected name to start with '{expected_name_prefix}'. Got {parsed_name} instead."
        )
    id = parsed_name[len(expected_name_prefix) :]
    return {"id": id, **parsed_configuration}


@dataclass(frozen=True)
class NumcodecsCodec:
    codec_config: dict[str, JSON]

    def __init__(
        self, *, codec_id: str | None = None, codec_config: dict[str, JSON]
    ) -> None:
        if "id" not in codec_config:
            if not codec_id:
                raise ValueError(
                    "The codec id needs to be supplied either through the id attribute "
                    "of the codec_config or through the codec_id argument."
                )
            codec_config = {"id": codec_id, **codec_config}
        elif codec_id and codec_config["id"] != codec_id:
            raise ValueError(
                f"Codec id does not match {codec_id}. Got: {codec_config['id']}."
            )

        object.__setattr__(self, "codec_config", codec_config)
        warn(
            "Numcodecs codecs are not in the Zarr version 3 specification and "
            "may not be supported by other zarr implementations.",
            category=UserWarning,
        )

    @cached_property
    def _codec(self) -> numcodecs.abc.Codec:
        print(self.codec_config)
        return numcodecs.get_codec(self.codec_config)

    @classmethod
    def from_dict(cls, data: dict[str, JSON]) -> Self:
        codec_config = parse_codec_configuration(data, CODEC_PREFIX)
        assert isinstance(codec_config["id"], str)  # for mypy
        return cls(codec_config=codec_config)

    def to_dict(self) -> JSON:
        codec_config = self.codec_config.copy()
        codec_id = codec_config.pop("id")
        return {
            "name": f"{CODEC_PREFIX}{codec_id}",
            "configuration": codec_config,
        }

    def compute_encoded_size(
        self, input_byte_length: int, chunk_spec: ArraySpec
    ) -> int:
        return input_byte_length


class NumcodecsBytesBytesCodec(NumcodecsCodec, BytesBytesCodec):
    def __init__(self, *, codec_id: str, codec_config: dict[str, JSON]) -> None:
        super().__init__(codec_id=codec_id, codec_config=codec_config)

    async def _decode_single(
        self, chunk_bytes: Buffer, _chunk_spec: ArraySpec
    ) -> Buffer:
        return await to_thread(as_numpy_array_wrapper, self._codec.decode, chunk_bytes)

    def _encode(self, chunk_bytes: Buffer) -> Buffer:
        encoded = self._codec.encode(chunk_bytes.as_array_like())
        if isinstance(encoded, np.ndarray):  # Required for checksum codecs
            return encoded.tobytes()
        return Buffer.from_bytes(encoded)

    async def _encode_single(
        self, chunk_bytes: Buffer, _chunk_spec: ArraySpec
    ) -> Buffer:
        return await to_thread(self._encode, chunk_bytes)


class NumcodecsArrayArrayCodec(NumcodecsCodec, ArrayArrayCodec):
    def __init__(self, *, codec_id: str, codec_config: dict[str, JSON]) -> None:
        super().__init__(codec_id=codec_id, codec_config=codec_config)

    async def _decode_single(
        self, chunk_array: NDBuffer, chunk_spec: ArraySpec
    ) -> NDBuffer:
        chunk_ndarray = chunk_array.as_ndarray_like()
        out = await to_thread(self._codec.decode, chunk_ndarray)
        return NDBuffer.from_ndarray_like(out.reshape(chunk_spec.shape))

    async def _encode_single(
        self, chunk_array: NDBuffer, _chunk_spec: ArraySpec
    ) -> NDBuffer:
        chunk_ndarray = chunk_array.as_ndarray_like()
        out = await to_thread(self._codec.encode, chunk_ndarray)
        return NDBuffer.from_ndarray_like(out)


def make_bytes_bytes_codec(
    codec_id: str, cls_name: str
) -> type[NumcodecsBytesBytesCodec]:
    # rename for class scope
    _codec_id = codec_id

    class _Codec(NumcodecsBytesBytesCodec):
        def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
            super().__init__(codec_id=_codec_id, codec_config=codec_config)

    _Codec.__name__ = cls_name
    return _Codec


def make_array_array_codec(
    codec_id: str, cls_name: str
) -> type[NumcodecsArrayArrayCodec]:
    # rename for class scope
    _codec_id = codec_id

    class _Codec(NumcodecsArrayArrayCodec):
        def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
            super().__init__(codec_id=_codec_id, codec_config=codec_config)

    _Codec.__name__ = cls_name
    return _Codec


def make_checksum_codec(codec_id: str, cls_name: str) -> type[NumcodecsBytesBytesCodec]:
    # rename for class scope
    _codec_id = codec_id

    class _ChecksumCodec(NumcodecsBytesBytesCodec):
        def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
            super().__init__(codec_id=_codec_id, codec_config=codec_config)

        def compute_encoded_size(
            self, input_byte_length: int, chunk_spec: ArraySpec
        ) -> int:
            return input_byte_length + 4

    _ChecksumCodec.__name__ = cls_name
    return _ChecksumCodec


class ShuffleCodec(NumcodecsBytesBytesCodec):
    def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
        super().__init__(codec_id="shuffle", codec_config=codec_config)

    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        if array_spec.dtype.itemsize != self.codec_config.get("elementsize"):
            return self.__class__(
                {**self.codec_config, "elementsize": array_spec.dtype.itemsize}
            )
        return self


class FixedScaleOffsetCodec(NumcodecsArrayArrayCodec):
    def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
        super().__init__(codec_id="fixedscaleoffset", codec_config=codec_config)

    def resolve_metadata(self, chunk_spec: ArraySpec) -> ArraySpec:
        if astype := self.codec_config.get("astype"):
            return replace(chunk_spec, dtype=np.dtype(astype))
        return chunk_spec

    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        if str(array_spec.dtype) != self.codec_config.get("dtype"):
            return self.__class__({**self.codec_config, "dtype": str(array_spec.dtype)})
        return self


class QuantizeCodec(NumcodecsArrayArrayCodec):
    def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
        super().__init__(codec_id="quantize", codec_config=codec_config)

    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        if str(array_spec.dtype) != self.codec_config.get("dtype"):
            return self.__class__({**self.codec_config, "dtype": str(array_spec.dtype)})
        return self


class AsTypeCodec(NumcodecsArrayArrayCodec):
    def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
        super().__init__(codec_id="astype", codec_config=codec_config)

    def resolve_metadata(self, chunk_spec: ArraySpec) -> ArraySpec:
        return replace(chunk_spec, dtype=np.dtype(self.codec_config["encode_dtype"]))

    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        decode_dtype = self.codec_config.get("decode_dtype")
        if str(array_spec.dtype) != decode_dtype:
            return self.__class__(
                {**self.codec_config, "decode_dtype": str(array_spec.dtype)}
            )
        return self


class PackbitsCodec(NumcodecsArrayArrayCodec):
    def __init__(self, codec_config: dict[str, JSON] = {}) -> None:
        super().__init__(codec_id="packbits", codec_config=codec_config)

    def resolve_metadata(self, chunk_spec: ArraySpec) -> ArraySpec:
        return replace(
            chunk_spec,
            shape=(1 + math.ceil(product(chunk_spec.shape) / 8),),
            dtype=np.dtype("uint8"),
        )

    def validate(self, array_metadata: ArrayMetadata) -> None:
        if array_metadata.dtype != np.dtype("bool"):
            raise ValueError(
                f"Packbits filter requires bool dtype. Got {array_metadata.dtype}."
            )


# bytes-to-bytes codecs
BloscCodec = make_bytes_bytes_codec("blosc", "BloscCodec")
Lz4Codec = make_bytes_bytes_codec("lz4", "Lz4Codec")
ZstdCodec = make_bytes_bytes_codec("zstd", "ZstdCodec")
ZlibCodec = make_bytes_bytes_codec("zlib", "ZlibCodec")
GzipCodec = make_bytes_bytes_codec("gzip", "GzipCodec")
Bz2Codec = make_bytes_bytes_codec("bz2", "Bz2Codec")
LzmaCodec = make_bytes_bytes_codec("lzma", "LzmaCodec")
# ShuffleCodec

# array-to-array codecs ("filters")
DeltaCodec = make_array_array_codec("delta", "DeltaCodec")
BitroundCodec = make_array_array_codec("bitround", "BitroundCodec")
# FixedScaleOffsetCodec
# QuantizeCodec
# PackbitsCodec
# AsTypeCodec

# bytes-to-bytes checksum codecs
Crc32Codec = make_checksum_codec("crc32", "Crc32Codec")
Adler32Codec = make_checksum_codec("adler32", "Adler32Codec")
Fletcher32Codec = make_checksum_codec("fletcher32", "Fletcher32Codec")
JenkinsLookup3 = make_checksum_codec("jenkins_lookup3", "JenkinsLookup3")