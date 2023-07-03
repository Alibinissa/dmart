#!/usr/bin/env python3.9
import argparse
import asyncio
from hashlib import blake2b, md5
import json
import os
import shutil
import sys
import copy
import jsonschema
from rich.console import Console
import sqlite3
from aiofiles import open as aopen
from pathlib import Path

console = Console()

def hashing_data(data: str):
    hash = blake2b(salt=md5(data.encode()).digest())
    hash.update(data.encode())
    return md5(hash.digest()).hexdigest()


def exit_with_error(msg: str):
    console.print(f"ERROR!!", style="#FF0B0B")
    console.print(msg, style="#FF4040")
    sys.exit(1)


SECURED_FIELDS = [
    "name",
    "email",
    "ip",
    "pin",
    "RechargeNumber",
    "CallingNumber",
    "shortname",
    "contact_number",
    "pin",
    "msisdn",
    "imsi",
    "sender_msisdn",
    "old_username",
    "firstname",
    "lastname",
]
OUTPUT_FOLDER_NAME = "spaces_data"


async def get_meta(
    *, 
    space_path: Path, 
    subpath: str, 
    file_path: str, 
    resource_type: str
):
    meta_content = space_path / f"{subpath}/.dm/{file_path}/meta.{resource_type}.json"
    async with aopen(meta_content, "r") as f:
        return json.loads(await f.read())


def validate_config(config_obj: dict):
    if (
        not config_obj.get("space")
        or not config_obj.get("subpath")
        or not config_obj.get("resource_type")
        or not config_obj.get("schema_shortname")
    ):
        return False
    return True


def remove_fields(src: dict, restricted_keys: list):
    for k in list(src.keys()):
        if type(src[k]) == list:
            for item in src[k]:
                if type(item) == dict:
                    item = remove_fields(item, restricted_keys)
        elif type(src[k]) == dict:
            src[k] = remove_fields(src[k], restricted_keys)
            
        if k in restricted_keys:
            del src[k]

    return src

def enc_dict(d: dict):
    for k, v in d.items():
        if type(v) is dict:
            d[k] = enc_dict(v)
        elif type(d[k]) == list:
            for item in d[k]:
                if type(item) == dict:
                    item = enc_dict(item)

        if k == "msisdn":
            d[k] = hashing_data(str(v))
            try:
                cur.execute(f"INSERT INTO migrated_data VALUES('{v}', '{d[k]}')")
                con.commit()
            except sqlite3.IntegrityError as e:
                if "migrated_data.hash" in str(e):  # msisdn already in db
                    raise Exception(f"Collision on: msisdn {v}, hash {d[k]}")
        elif k in SECURED_FIELDS:
            d[k] = hashing_data(str(v))

    return d


def prepare_output(
    meta: dict,
    payload: dict,
    included_meta_fields: dict,
    excluded_payload_fields: dict,
):
    out = payload
    for field_meta in included_meta_fields:
        field_name = field_meta.get("field_name")
        rename_to = field_meta.get("rename_to")
        if not field_name:
            continue
        if rename_to:
            out[rename_to] = meta.get(field_name, "")
        else:
            out[field_name] = meta.get(field_name, "")

    out = remove_fields(
        out, 
        [field["field_name"] for field in excluded_payload_fields]
    )
    return out


                

async def extract(config_obj, spaces_path, output_path):
    space = config_obj.get("space")
    subpath = config_obj.get("subpath")
    resource_type = config_obj.get("resource_type")
    schema_shortname = config_obj.get("schema_shortname")
    included_meta_fields = config_obj.get("included_meta_fields", [])
    excluded_payload_fields = config_obj.get("excluded_payload_fields", [])

    space_path = Path(f"{spaces_path}/{space}")
    subpath_schema_obj = None
    with open(space_path / f"schema/{schema_shortname}.json", "r") as f:
        subpath_schema_obj = json.load(f)
    input_subpath_schema_obj = copy.deepcopy(subpath_schema_obj)

    output_subpath = Path(f"{output_path}/{OUTPUT_FOLDER_NAME}/{space}/{subpath}")
    if not output_subpath.is_dir():
        os.makedirs(output_subpath)

    # Generat output schema
    schema_fil = output_subpath / f"schema.json"
    for field in included_meta_fields:
        subpath_schema_obj["properties"][field["field_name"]] = field["schema_entry"]
        if field.get("rename_to"):
            subpath_schema_obj["properties"][field["rename_to"]] = subpath_schema_obj[
                "properties"
            ].pop(field["field_name"])
    subpath_schema_obj["properties"] = remove_fields(
        subpath_schema_obj["properties"], 
        [field["field_name"] for field in excluded_payload_fields]
    )
    open(schema_fil, "w").write(json.dumps(subpath_schema_obj) + "\n")

    # Generat output content file
    data_file = output_subpath / f"data.ljson"
    path = os.path.join(spaces_path, space, subpath)
    for file_name in os.listdir(path):
        if not file_name.endswith(".json"):
            continue
        async with aopen(os.path.join(path, file_name), "r") as f:
            content = await f.read()
        try:
            payload = json.loads(content)
            jsonschema.validate(
                instance=payload, schema=input_subpath_schema_obj
            )
            meta = await get_meta(
                space_path=space_path,
                subpath=subpath,
                file_path=file_name.split(".")[0],
                resource_type=resource_type,
            )
        except Exception as error:
            print(f"{error=}")
            continue

        out = prepare_output(
            meta, payload, included_meta_fields, excluded_payload_fields
        )

        # jsonschema.validate(instance=out, schema=subpath_schema_obj)

        encrypted_out = enc_dict(out)
        open(data_file, "a").write(json.dumps(encrypted_out) + "\n")


async def main(tasks):
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", required=True, help="Json config relative path from the script"
    )
    parser.add_argument(
        "--spaces", required=True, help="Spaces relative path from the script"
    )
    parser.add_argument(
        "--output",
        help="Output relative path from the script (the default path is the current script path)",
    )
    args = parser.parse_args()
    output_path = ""
    if args.output:
        output_path = args.output

    if not os.path.isdir(args.spaces):
        exit_with_error(f"The spaces folder {args.spaces} is not found.")

    out_path = os.path.join(output_path, OUTPUT_FOLDER_NAME)
    if os.path.isdir(out_path):
        shutil.rmtree(out_path)

    con = sqlite3.connect(f"/tmp/data.db")
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS migrated_data")
    cur.execute(
        "CREATE TABLE migrated_data(hash VARCHAR UNIQUE, msisdn VARCHAR UNIQUE)"
    )

    tasks = []
    with open(args.config, "r") as f:
        config_objs = json.load(f)

    for config_obj in config_objs:
        if not validate_config(config_obj):
            continue
        tasks.append(extract(config_obj, args.spaces, output_path))

    asyncio.run(main(tasks))

    console.print(
        f"Output path: {os.path.abspath(os.path.join(output_path, OUTPUT_FOLDER_NAME))}",
        style="#6EE853",
    )
