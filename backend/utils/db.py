from copy import copy
import shutil
import sys
from utils.helpers import (
    arr_remove_common,
    branch_path,
    camel_case,
    flatten_all,
    snake_case,
    str_to_datetime,
)
from datetime import datetime
from models.enums import ContentType, ResourceType
from utils.internal_error_code import InternalErrorCode
from utils.middleware import get_request_data
from utils.settings import settings
import models.core as core
from typing import TypeVar, Any
import models.api as api
import os
import json
from pathlib import Path
from fastapi import status
import aiofiles
from utils.regex import ATTACHMENT_PATTERN, FILE_PATTERN, FOLDER_PATTERN
from shutil import copy2 as copy_file
from fastapi.logger import logger

MetaChild = TypeVar("MetaChild", bound=core.Meta)


def locators_query(query: api.Query) -> tuple[int, list[core.Locator]]:
    """Given a query return the total and the locators
    Parameters
    ----------
    query: api.Query
        Query of type subpath

    Returns
    -------
    Total, Locators

    """

    locators: list[core.Locator] = []
    total: int = 0
    match query.type:
        case api.QueryType.subpath:
            path = (
                settings.spaces_folder
                / query.space_name
                / branch_path(query.branch_name)
                / query.subpath
            )

            if query.include_fields is None:
                query.include_fields = []

            # Gel all matching entries
            meta_path = path / ".dm"
            if not meta_path.is_dir():
                return total, locators

            path_iterator = os.scandir(meta_path)
            for entry in path_iterator:
                if not entry.is_dir():
                    continue

                subpath_iterator = os.scandir(entry)
                for one in subpath_iterator:
                    # for one in path.glob(entries_glob):
                    match = FILE_PATTERN.search(str(one.path))
                    if not match or not one.is_file():
                        continue

                    total += 1
                    if len(locators) >= query.limit or total < query.offset:
                        continue

                    shortname = match.group(1)
                    resource_name = match.group(2).lower()
                    if (
                        query.filter_types
                        and ResourceType(resource_name) not in query.filter_types
                    ):
                        continue

                    if (
                        query.filter_shortnames
                        and shortname not in query.filter_shortnames
                    ):
                        continue

                    locators.append(
                        core.Locator(
                            space_name=query.space_name,
                            branch_name=query.branch_name,
                            subpath=query.subpath,
                            shortname=shortname,
                            type=ResourceType(resource_name),
                        )
                    )

            # Get all matching sub folders
            subfolders_iterator = os.scandir(path)
            for one in subfolders_iterator:
                if not one.is_dir():
                    continue

                subfolder_meta = Path(one.path + "/.dm/meta.folder.json")

                match = FOLDER_PATTERN.search(str(subfolder_meta))

                if not match or not subfolder_meta.is_file():
                    continue

                total += 1
                if len(locators) >= query.limit or total < query.offset:
                    continue

                shortname = match.group(1)
                if query.filter_shortnames and shortname not in query.filter_shortnames:
                    continue

                locators.append(
                    core.Locator(
                        space_name=query.space_name,
                        branch_name=query.branch_name,
                        subpath=query.subpath,
                        shortname=shortname,
                        type=core.ResourceType.folder,
                    )
                )

    return total, locators


def folder_path(
    space_name: str,
    subpath: str,
    shortname: str,
    branch_name: str | None = settings.default_branch,
):
    if branch_name:
        return (
            f"{settings.spaces_folder}/{space_name}/{branch_name}/{subpath}/{shortname}"
        )
    else:
        return f"{settings.spaces_folder}/{space_name}{subpath}/{shortname}"


async def get_entry_attachments(
    subpath: str,
    attachments_path: Path,
    branch_name: str | None = None,
    filter_types: list | None = None,
    include_fields: list | None = None,
    filter_shortnames: list | None = None,
    retrieve_json_payload: bool = False,
) -> dict:
    if not attachments_path.is_dir():
        return {}
    attachments_iterator = os.scandir(attachments_path)
    attachments_dict: dict[str, list] = {}
    for attachment_entry in attachments_iterator:
        # TODO: Filter types on the parent attachment type folder layer
        if not attachment_entry.is_dir():
            continue

        attachments_files = os.scandir(attachment_entry)
        for attachments_file in attachments_files:
            match = ATTACHMENT_PATTERN.search(str(attachments_file.path))
            if not match or not attachments_file.is_file():
                continue

            attach_shortname = match.group(2)
            attach_resource_name = match.group(1).lower()
            if filter_shortnames and attach_shortname not in filter_shortnames:
                continue

            if filter_types and ResourceType(attach_resource_name) not in filter_types:
                continue

            resource_class = getattr(
                sys.modules["models.core"], camel_case(attach_resource_name)
            )
            resource_obj = None
            async with aiofiles.open(attachments_file, "r") as meta_file:
                try:
                    resource_obj = resource_class.model_validate_json(
                        await meta_file.read()
                    )
                except Exception as e:
                    raise Exception(f"Bad attachment ... {attachments_file=}") from e

            resource_record_obj = resource_obj.to_record(
                subpath, attach_shortname, include_fields, branch_name
            )
            if (
                retrieve_json_payload
                and resource_obj
                and resource_record_obj
                and resource_obj.payload
                and resource_obj.payload.content_type
                and resource_obj.payload.content_type == ContentType.json
                and Path(
                    f"{attachment_entry.path}/{resource_obj.payload.body}"
                ).is_file()
            ):
                async with aiofiles.open(
                    f"{attachment_entry.path}/{resource_obj.payload.body}", "r"
                ) as payload_file_content:
                    resource_record_obj.attributes["payload"].body = json.loads(
                        await payload_file_content.read()
                    )

            if attach_resource_name in attachments_dict:
                attachments_dict[attach_resource_name].append(resource_record_obj)
            else:
                attachments_dict[attach_resource_name] = [resource_record_obj]
        attachments_files.close()
    attachments_iterator.close()

    # SORT ALTERATION ATTACHMENTS BY ALTERATION.CREATED_AT
    for attachment_name, attachments in attachments_dict.items():
        try:
            if attachment_name == ResourceType.alteration:
                attachments_dict[attachment_name] = sorted(
                    attachments, key=lambda d: d.attributes["created_at"]
                )
        except Exception as e:
            logger.error(
                f"Invalid attachment entry:{attachments_path/attachment_name}. Error: {e.args}"
            )

    return attachments_dict


def metapath(entity: core.EntityDTO) -> tuple[Path, str]:
    """Construct the full path of the meta file"""
    path = settings.spaces_folder / entity.space_name / branch_path(entity.branch_name)

    filename = ""
    if entity.subpath[0] == "/":
        entity.subpath = f".{entity.subpath}"
    if issubclass(entity.class_type, core.Folder):
        path = path / entity.subpath / entity.shortname / ".dm"
        filename = f"meta.{entity.class_type.__name__.lower()}.json"
    elif issubclass(entity.class_type, core.Space):
        path = settings.spaces_folder / entity.space_name / ".dm"
        filename = "meta.space.json"
    elif issubclass(entity.class_type, core.Attachment):
        [parent_subpath, parent_name] = entity.subpath.rsplit("/", 1)
        # schema_shortname = "." + schema_shortname if schema_shortname else ""
        attachment_folder = (
            f"{parent_name}/attachments.{entity.class_type.__name__.lower()}"
        )
        path = path / parent_subpath / ".dm" / attachment_folder
        filename = f"meta.{entity.shortname}.json"
    elif issubclass(entity.class_type, core.History):
        [parent_subpath, parent_name] = entity.subpath.rsplit("/", 1)
        path = path / parent_subpath / ".dm" / f"{parent_name}/history"
        filename = f"{entity.shortname}.json"
    elif issubclass(entity.class_type, core.Branch):
        path = settings.spaces_folder / entity.space_name / entity.shortname / ".dm"
        filename = "meta.branch.json"
    else:
        path = path / entity.subpath / ".dm" / entity.shortname
        filename = f"meta.{snake_case(entity.class_type.__name__)}.json"
    return path, filename


def payload_path(entity: core.EntityDTO) -> Path:
    """Construct the full path of the meta file"""
    path = settings.spaces_folder / entity.space_name / branch_path(entity.branch_name)

    if entity.subpath[0] == "/":
        entity.subpath = f".{entity.subpath}"
    if issubclass(entity.class_type, core.Attachment):
        [parent_subpath, parent_name] = entity.subpath.rsplit("/", 1)
        schema_shortname = (
            "." + entity.schema_shortname if entity.schema_shortname else ""
        )
        attachment_folder = f"{parent_name}/attachments{schema_shortname}.{entity.class_type.__name__.lower()}"
        path = path / parent_subpath / ".dm" / attachment_folder
    else:
        path = path / entity.subpath
    return path


async def load_or_none(entity: core.EntityDTO) -> MetaChild | None:  # type: ignore
    """Load a Meta Json according to the reuqested Class type"""
    path, filename = metapath(entity)
    if not (path / filename).is_file():
        # Remove the folder
        if path.is_dir() and len(os.listdir(path)) == 0:
            shutil.rmtree(path)

        return None

    path /= filename
    async with aiofiles.open(path, "r") as file:
        content = await file.read()
        return entity.class_type.model_validate_json(content)


async def load(entity: core.EntityDTO) -> MetaChild:  # type: ignore
    meta = await load_or_none(entity)
    if not meta:
        raise api.Exception(
            status_code=status.HTTP_404_NOT_FOUND,
            error=api.Error(
                type="db",
                code=InternalErrorCode.OBJECT_NOT_FOUND,
                message=f"Request object is not available @{entity.space_name}/{entity.subpath}/{entity.shortname} {entity.resource_type=} {entity.schema_shortname=}",
            ),
        )

    return meta


async def load_resource_payload(entity: core.EntityDTO) -> dict[str, Any]:
    """Load a Meta class payload file"""

    path = payload_path(entity)

    meta = await load(entity)

    if not meta:
        return {}

    if not meta.payload or not isinstance(meta.payload.body, str):
        return {}

    path /= meta.payload.body
    if not path.is_file():
        return {}

    async with aiofiles.open(path, "r") as file:
        content = await file.read()
        return json.loads(content)


async def save(
    entity: core.EntityDTO, meta: core.Meta, payload_data: dict[str, Any] | None = None
):
    """Save Meta Json to respectiv file"""
    path, filename = metapath(entity)

    if not path.is_dir():
        os.makedirs(path)

    async with aiofiles.open(path / filename, "w") as file:
        await file.write(meta.model_dump_json(exclude_none=True))

    if payload_data:
        payload_file_path = payload_path(entity)

        payload_filename = f"{meta.shortname}.json"

        async with aiofiles.open(payload_file_path / payload_filename, "w") as file:
            await file.write(json.dumps(payload_data))


async def create(
    entity: core.EntityDTO, meta: core.Meta, payload_data: dict[str, Any] | None = None
):
    path, filename = metapath(entity)

    if (path / filename).is_file():
        raise api.Exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            error=api.Error(
                type="create",
                code=InternalErrorCode.SHORTNAME_ALREADY_EXIST,
                message="already exists",
            ),
        )

    await save(entity, meta, payload_data)


async def save_payload(entity: core.EntityDTO, meta: core.Meta, attachment):
    path, filename = metapath(entity)
    payload_file_path = payload_path(entity)
    payload_filename = meta.shortname + Path(attachment.filename).suffix

    if not (path / filename).is_file():
        raise api.Exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            error=api.Error(
                type="create",
                code=InternalErrorCode.MISSING_METADATA,
                message="metadata is missing",
            ),
        )

    async with aiofiles.open(payload_file_path / payload_filename, "wb") as file:
        content = await attachment.read()
        await file.write(content)


async def save_payload_from_json(
    entity: core.EntityDTO, meta: core.Meta, payload_data: dict[str, Any]
):
    path, filename = metapath(entity)
    payload_file_path = payload_path(entity)

    payload_filename = f"{meta.shortname}.json"

    if not (path / filename).is_file():
        raise api.Exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            error=api.Error(
                type="create",
                code=InternalErrorCode.MISSING_METADATA,
                message="metadata is missing",
            ),
        )

    async with aiofiles.open(payload_file_path / payload_filename, "w") as file:
        await file.write(json.dumps(payload_data))


async def update(
    entity: core.EntityDTO, meta: core.Meta, payload_data: dict[str, Any] | None = None
) -> dict:
    """Update the entry, store the difference and return it
    1. load the current file
    3. store meta at the file location
    4. store the diff between old and new file
    """
    old_meta = await load(entity)
    old_payload = await load_resource_payload(entity)

    meta.updated_at = datetime.now()

    await save(entity, meta, payload_data)

    history_diff = await store_entry_diff(
        entity=entity,
        old_meta=old_meta,
        new_meta=meta,
        old_payload=old_payload,
        new_payload=payload_data,
    )

    return history_diff


async def store_entry_diff(
    entity: core.EntityDTO,
    old_meta: core.Meta,
    new_meta: core.Meta,
    old_payload: dict[str, Any] | None = None,
    new_payload: dict[str, Any] | None = None,
) -> dict:
    old_flattened = flatten_all(old_meta.model_dump(exclude_none=True))
    if old_payload:
        old_flattened.update(flatten_all(old_payload))

    new_flattened = flatten_all(new_meta.model_dump(exclude_none=True))
    if new_payload:
        new_flattened.update(flatten_all(new_payload))

    diff_keys = list(old_flattened.keys())
    diff_keys.extend(list(new_flattened.keys()))
    history_diff = {}
    for key in set(diff_keys):
        if key in ["updated_at"]:
            continue
        # if key in updated_attributes_flattend:
        old = copy(old_flattened.get(key, "null"))

        new = copy(new_flattened.get(key, "null"))

        if old != new:
            if isinstance(old, list) and isinstance(new, list):
                old, new = arr_remove_common(old, new)
            history_diff[key] = {
                "old": old,
                "new": new,
            }
    if not history_diff:
        return {}

    history_obj = core.History(
        shortname="history",
        owner_shortname=entity.user_shortname or "__system__",
        timestamp=datetime.now(),
        request_headers=get_request_data().get("request_headers", {}),
        diff=history_diff,
    )
    history_path = (
        settings.spaces_folder / entity.space_name / branch_path(entity.branch_name)
    )

    if entity.subpath == "/" and entity.resource_type == core.Space:
        history_path = Path(f"{history_path}/.dm")
    else:
        if issubclass(entity.class_type, core.Attachment):
            history_path = Path(f"{history_path}/.dm/{entity.subpath}")
        else:
            if entity.subpath == "/":
                history_path = Path(f"{history_path}/.dm/{entity.shortname}")
            else:
                history_path = Path(
                    f"{history_path}/{entity.subpath}/.dm/{entity.shortname}"
                )

    if not os.path.exists(history_path):
        os.makedirs(history_path)

    async with aiofiles.open(
        f"{history_path}/history.jsonl",
        "a",
    ) as events_file:
        await events_file.write(f"{history_obj.model_dump_json()}\n")

    return history_diff


async def move(
    entity: core.EntityDTO,
    meta: core.Meta,
    dest_subpath: str | None,
    dest_shortname: str | None,
):
    """Move the file that match the criteria given, remove source folder if empty"""
    dest_entity = copy(entity)

    if dest_subpath:
        dest_entity.subpath = dest_subpath
    if dest_shortname:
        dest_entity.shortname = dest_shortname

    src_path, src_filename = metapath(entity)
    dest_path, dest_filename = metapath(dest_entity)

    meta_updated = False
    dest_path_without_dm = dest_path
    if dest_shortname:
        meta.shortname = dest_shortname
        meta_updated = True

    if src_path.parts[-1] == ".dm":
        src_path = Path("/".join(src_path.parts[:-1]))

    if dest_path.parts[-1] == ".dm":
        dest_path_without_dm = Path("/".join(dest_path.parts[:-1]))

    if dest_path_without_dm.is_dir() and len(os.listdir(dest_path_without_dm)):
        raise api.Exception(
            status_code=status.HTTP_404_NOT_FOUND,
            error=api.Error(
                type="move",
                code=InternalErrorCode.NOT_ALLOWED_LOCATION,
                message="The destination folder is not empty",
            ),
        )

    # Create dest dir if there's a change in the subpath AND the shortname
    # and the subpath shortname folder doesn't exist,
    if (
        entity.shortname != dest_shortname
        and entity.subpath != dest_subpath
        and not os.path.isdir(dest_path_without_dm)
    ):
        os.makedirs(dest_path_without_dm)

    os.rename(src=src_path, dst=dest_path_without_dm)

    # Move payload file with the meta file
    if (
        meta.payload
        and meta.payload.content_type != ContentType.text
        and isinstance(meta.payload.body, str)
    ):
        src_payload_file_path = payload_path(entity) / meta.payload.body
        meta.payload.body = meta.shortname + "." + meta.payload.body.split(".")[-1]
        dist_payload_file_path = payload_path(dest_entity) / meta.payload.body
        if src_payload_file_path.is_file():
            os.rename(src=src_payload_file_path, dst=dist_payload_file_path)

    if meta_updated:
        async with aiofiles.open(dest_path / dest_filename, "w") as opened_file:
            await opened_file.write(meta.model_dump_json(exclude_none=True))

    # Delete Src path if empty
    if src_path.parent.is_dir():
        delete_empty(src_path)


def delete_empty(path: Path):
    if path.is_dir() and len(os.listdir(path)) == 0:
        os.removedirs(path)

    if path.parent.is_dir() and len(os.listdir(path.parent)) == 0:
        delete_empty(path.parent)


async def clone(
    src_entity: core.EntityDTO,
    dest_entity: core.EntityDTO,
):

    meta_obj = await load(src_entity)

    src_path, src_filename = metapath(src_entity)
    dest_path, dest_filename = metapath(dest_entity)

    # Create dest dir if not exist
    if not os.path.isdir(dest_path):
        os.makedirs(dest_path)

    copy_file(src=src_path / src_filename, dst=dest_path / dest_filename)

    # Move payload file with the meta file
    if (
        meta_obj.payload
        and meta_obj.payload.content_type != ContentType.text
        and isinstance(meta_obj.payload.body, str)
    ):
        src_payload_file_path = payload_path(src_entity) / meta_obj.payload.body
        dist_payload_file_path = payload_path(dest_entity) / meta_obj.payload.body
        copy_file(src=src_payload_file_path, dst=dist_payload_file_path)


async def delete(entity: core.EntityDTO):
    """Delete the file that match the criteria given, remove folder if empty"""

    path, filename = metapath(entity)
    if not path.is_dir() or not (path / filename).is_file():
        raise api.Exception(
            status_code=status.HTTP_404_NOT_FOUND,
            error=api.Error(
                type="delete",
                code=InternalErrorCode.OBJECT_NOT_FOUND,
                message="Request object is not available",
            ),
        )

    meta = await load(entity)

    pathname = path / filename
    if pathname.is_file():
        os.remove(pathname)

        # Delete payload file
        if meta.payload and meta.payload.content_type not in ContentType.inline_types():
            payload_file_path = payload_path(entity) / str(meta.payload.body)
            if payload_file_path.exists() and payload_file_path.is_file():
                os.remove(payload_file_path)

    history_path = (
        f"{settings.spaces_folder}/{entity.space_name}/{branch_path(entity.branch_name)}"
        + f"{entity.subpath}/.dm/{meta.shortname}"
    )

    if path.is_dir() and (
        not isinstance(meta, core.Attachment) or len(os.listdir(path)) == 0
    ):
        shutil.rmtree(path)
        # in case of folder the path = {folder_name}/.dm
        if isinstance(meta, core.Folder) and path.parent.is_dir():
            shutil.rmtree(path.parent)
        if isinstance(meta, core.Folder) and Path(history_path).is_dir():
            shutil.rmtree(history_path)


def is_entry_exist(entity: core.EntityDTO) -> bool:
    if entity.subpath[0] == "/":
        entity.subpath = f".{entity.subpath}"

    payload_file = (
        settings.spaces_folder
        / entity.space_name
        / branch_path(entity.branch_name)
        / entity.subpath
        / f"{entity.shortname}.json"
    )
    if payload_file.is_file():
        return True

    for r_type in ResourceType:
        # Spaces compared with each others only
        if r_type == ResourceType.space and r_type != entity.resource_type:
            continue
        meta_path, meta_file = metapath(entity)
        if (meta_path / meta_file).is_file():
            return True

    return False
