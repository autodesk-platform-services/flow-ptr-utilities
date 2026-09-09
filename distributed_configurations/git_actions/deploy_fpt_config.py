#!/usr/bin/env python3

import argparse
import os
import sys
import time
from pathlib import Path

import shotgun_api3


FIELD_NAME = "uploaded_config"
ENTITY_TYPE = "PipelineConfiguration"

MAX_UPLOAD_ATTEMPTS = 3


def get_required_env(name):
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set."
        )

    return value


def create_fpt_connection():
    base_url = get_required_env("FPT_BASE_URL")
    script_name = get_required_env("FPT_SCRIPT_NAME")
    script_key = get_required_env("FPT_SCRIPT_KEY")

    print(f"Connecting to Flow Production Tracking: {base_url}")

    return shotgun_api3.Shotgun(
        base_url,
        script_name=script_name,
        api_key=script_key,
    )


def get_pipeline_configuration(sg, pipeline_config_id):
    print(
        f"Looking up PipelineConfiguration "
        f"{pipeline_config_id}..."
    )

    config = sg.find_one(
        ENTITY_TYPE,
        [["id", "is", pipeline_config_id]],
        [
            "id",
            "code",
            "project",
            FIELD_NAME,
        ],
    )

    if not config:
        raise RuntimeError(
            f"PipelineConfiguration {pipeline_config_id} "
            f"was not found."
        )

    return config


def upload_config(
    sg,
    pipeline_config_id,
    zip_path,
    display_name,
):
    for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):

        try:
            print(
                f"Uploading configuration "
                f"(attempt {attempt}/{MAX_UPLOAD_ATTEMPTS})..."
            )

            attachment_id = sg.upload(
                ENTITY_TYPE,
                pipeline_config_id,
                str(zip_path),
                field_name=FIELD_NAME,
                display_name=display_name,
            )

            print(
                f"Upload successful. "
                f"Attachment ID: {attachment_id}"
            )

            return attachment_id

        except Exception as exc:

            print(
                f"Upload attempt {attempt} failed: {exc}",
                file=sys.stderr,
            )

            if attempt == MAX_UPLOAD_ATTEMPTS:
                raise

            wait_seconds = attempt * 3

            print(
                f"Retrying in {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)


def verify_upload(sg, pipeline_config_id):
    print("Verifying PipelineConfiguration...")

    config = sg.find_one(
        ENTITY_TYPE,
        [["id", "is", pipeline_config_id]],
        [
            "id",
            "code",
            FIELD_NAME,
        ],
    )

    if not config:
        raise RuntimeError(
            "PipelineConfiguration disappeared "
            "during deployment."
        )

    uploaded_config = config.get(FIELD_NAME)

    if not uploaded_config:
        raise RuntimeError(
            "Deployment completed but uploaded_config "
            "is empty."
        )

    print("Deployment verification successful.")

    print(
        f"Pipeline Configuration: "
        f"{config.get('code')}"
    )

    print(
        f"Attachment ID: "
        f"{uploaded_config.get('id')}"
    )

    print(
        f"Attachment name: "
        f"{uploaded_config.get('name')}"
    )

    return uploaded_config


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Deploy a Toolkit distributed configuration "
            "to Flow Production Tracking."
        )
    )

    parser.add_argument(
        "zip_file",
        help="Path to the Toolkit configuration ZIP.",
    )

    parser.add_argument(
        "--version",
        required=True,
        help="Git release/tag version.",
    )

    args = parser.parse_args()

    zip_path = Path(args.zip_file)

    if not zip_path.exists():
        raise RuntimeError(
            f"Configuration ZIP does not exist: {zip_path}"
        )

    if zip_path.suffix.lower() != ".zip":
        raise RuntimeError(
            f"Expected a ZIP file: {zip_path}"
        )

    pipeline_config_id = int(
        get_required_env("FPT_PIPELINE_CONFIG_ID")
    )

    print("")
    print("=" * 60)
    print("FPT CONFIGURATION DEPLOYMENT")
    print("=" * 60)
    print(f"Version:       {args.version}")
    print(f"ZIP:           {zip_path}")
    print(f"Size:          {zip_path.stat().st_size} bytes")
    print(f"Pipeline ID:   {pipeline_config_id}")
    print("=" * 60)
    print("")

    sg = create_fpt_connection()

    config = get_pipeline_configuration(
        sg,
        pipeline_config_id,
    )

    print(
        f"Target configuration: "
        f"{config.get('code')}"
    )

    display_name = (
        f"fpt-config-{args.version}.zip"
    )

    attachment_id = upload_config(
        sg,
        pipeline_config_id,
        zip_path,
        display_name,
    )

    uploaded_config = verify_upload(
        sg,
        pipeline_config_id,
    )

    print("")
    print("=" * 60)
    print("DEPLOYMENT SUCCESSFUL")
    print("=" * 60)
    print(f"Version:       {args.version}")
    print(f"Attachment:    {attachment_id}")
    print(
        f"FPT attachment: "
        f"{uploaded_config.get('name')}"
    )
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"DEPLOYMENT FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)