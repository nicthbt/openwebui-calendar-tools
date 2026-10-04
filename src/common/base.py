import io
import logging
import mimetypes
import os
import re
import unicodedata
from contextvars import ContextVar
from difflib import SequenceMatcher
from hashlib import blake2b

from fastapi import Request, UploadFile
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.users import UserModel
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.routers.files import upload_file_handler
from open_webui.routers.retrieval import (
    ProcessFileForm,
    QueryCollectionsForm,
    process_file,
    query_collection_handler,
)

log = logging.getLogger(__name__)


class CustomTool:
    def __init__(self, namespace):
        self.valves = self.Valves()
        self.context = ContextVar(namespace)
        self.namespace = namespace
        # Add missing mimetypes
        msoffice = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        libreoffice = {
            ".odt": "application/vnd.oasis.opendocument.text",
            ".ods": "application/vnd.oasis.opendocument.spreadsheet",
            ".odp": "application/vnd.oasis.opendocument.presentation",
        }
        for extension, mimetype in msoffice.items():
            mimetypes.add_type(mimetype, extension)
        for extension, mimetype in libreoffice.items():
            mimetypes.add_type(mimetype, extension)

    def _is_media(
        self,
        mimetype: str,
        checklist: list = ["image/", "audio/", "video/"],
    ) -> bool:
        if mimetype is not None:
            return mimetype.startswith(tuple(checklist))
        return False

    def _seq_match(self, text: str, keywords: list) -> list:
        # Normalize text in ascii characters
        nfkd_text = (
            unicodedata.normalize("NFKD", text.lower())
            .encode("ascii", "ignore")
            .decode()
        )
        # Normalize keywords in ascii characters
        nfkd_keywords = [
            unicodedata.normalize("NFKD", keyword.lower())
            .encode("ascii", "ignore")
            .decode()
            for keyword in keywords
        ]

        # Get the match length for each keyword
        return [
            SequenceMatcher(None, nfkd_keyword, nfkd_text).find_longest_match().size
            for nfkd_keyword in nfkd_keywords
        ]

    def _sort_results(self, results: list, keys: list) -> list:
        # Sort by keys from lowest to highest priority
        for key, reverse in reversed(keys):
            results.sort(key=lambda result: result[key], reverse=reverse)
        return results[: self.valves.search_count]

    def _extract_keywords(self, query: str) -> list:
        if query is None or len(query.strip()) == 0:
            return []

        # Split query on special characters (space, tab, comma, etc) and remove linking words
        keywords = set(
            keyword.strip()
            for keyword in re.split(r"[';,\s\t\r\n]+", query)
            if len(keyword.strip()) > 1
        )

        # Check that keywords are not empty
        if len(keywords) == 0:
            raise ValueError(f"Cannot build keywords from query string '{query}'")

        return list(keywords)

    async def _query_collections(
        self,
        query: str,
        collections: list,
        __user__: dict,
        __request__: Request,
    ) -> list:
        # Query the collection using the retrieval engine
        collection_results = await query_collection_handler(
            __request__,
            QueryCollectionsForm(
                collection_names=collections,
                query=query,
            ),
            user=UserModel(**__user__),
        )

        results = {}

        # Generate query-focused results (instead of relying on raw results)
        for distances, metadatas, documents in zip(
            collection_results.get("distances", []),
            collection_results.get("metadatas", []),
            collection_results.get("documents", []),
        ):
            for distance, metadata, document in zip(distances, metadatas, documents):
                file_id = metadata.get("file_id")
                file_metadata = await Files.get_file_metadata_by_id(file_id)
                source = file_metadata.meta.get("source") or metadata.get("source")
                source_hash = blake2b(source.encode()).hexdigest()
                # Add new source to results or update existing source with new snippets
                snippets = results.get(source_hash, {}).get("snippets", [])
                snippets.append(document)
                # Add new source to results or update existing source with new snippets
                results.update(
                    {
                        source_hash: {
                            "id": file_id,
                            "source": source,
                            "snippets": snippets,
                        }
                    }
                )

        return list(results.values())

    async def _get_cache_file(
        self,
        file_hash: str,
        user: UserModel,
    ) -> tuple:
        cache_key = f"{self.namespace}.files.{user.id}.{file_hash}"
        cache_value = await Config.get(cache_key, {})

        file_id = cache_value.get("id", None)
        file_collection = cache_value.get("collection", None)

        cleanup = False

        if file_id is not None:
            file = await Files.get_file_by_id(file_id)
            if not file:
                cleanup = True

        if file_collection is not None:
            collection = await ASYNC_VECTOR_DB_CLIENT.has_collection(file_collection)
            if not collection:
                cleanup = True

        # Delete cache if file or collection no longer exist
        if cleanup:
            log.warning(f"Deleting cache for {cache_key}")
            await Config.delete(cache_key)
            return None, None

        return file_id, file_collection

    async def _set_cache_file(
        self,
        file_hash: str,
        file_id: str,
        file_collection: str,
        user: UserModel,
    ) -> None:
        cache_key = f"{self.namespace}.files.{user.id}.{file_hash}"
        cache_value = {
            "id": file_id,
            "collection": file_collection,
        }
        await Config.upsert({cache_key: cache_value})

    async def _upload_file(
        self,
        source: str,
        filename: str,
        mimetype: str,
        content: bytes,
        process: bool,
        __user__: dict,
        __request__: Request,
    ) -> tuple:
        async with get_async_db_context() as db:
            user = UserModel(**__user__)

            # Search for file in cache
            file_hash = blake2b(source.encode() + b"\0" + content).hexdigest()
            file_id, file_collection = await self._get_cache_file(
                file_hash,
                user=user,
            )

            # Upload file if not in cache
            if file_id is None:
                log.info(f"Uploading '{filename}'")
                file = await upload_file_handler(
                    __request__,
                    UploadFile(
                        file=io.BytesIO(content),
                        filename=filename,
                        headers={"content-type": mimetype},
                    ),
                    metadata={},
                    process=False,
                    user=user,
                    db=db,
                )
                file_id = file.id

            # Update source
            await Files.update_file_metadata_by_id(
                file_id,
                {"source": source},
                db=db,
            )

            # Process file if not in cache
            if file_collection is None and process is True:
                log.info(f"Processing '{filename}'")
                result = await process_file(
                    __request__,
                    ProcessFileForm(file_id=file_id),
                    user=user,
                    db=db,
                )
                file_collection = result.get("collection_name")

            await self._set_cache_file(
                file_hash,
                file_id,
                file_collection,
                user=user,
            )

            return file_id, file_collection

    async def _emit_sources(
        self,
        event_emitter,
        files: list,
    ) -> None:
        for file in files:
            file_id = file.get("id")
            source = file.get("source")
            filename = os.path.basename(source)
            snippets = file.get("snippets")
            if event_emitter:
                await event_emitter(
                    {
                        "type": "source",
                        "data": {
                            "source": {
                                "id": file_id,
                                "name": filename,
                                "type": "file",
                            },
                            "document": snippets,
                            "metadata": [
                                {
                                    "file_id": file_id,
                                    "name": filename,
                                    "source": source,
                                }
                                for snippet in snippets
                            ],
                        },
                    }
                )

    async def _emit_files(
        self,
        event_emitter,
        files: list,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "files",
                    "data": {
                        "files": [
                            (
                                {**file, "type": "filesystem"}
                                if file.get("id", None) is None
                                else {**file, "type": "file"}
                            )
                            for file in files
                        ],
                    },
                }
            )

    async def _emit_status(
        self,
        event_emitter,
        desc: str,
        done: bool = False,
        hidden: bool = False,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": desc,
                        "done": done,
                        "hidden": hidden,
                    },
                }
            )
