# Copyright 2018 Databricks, Inc.
import re

DEV_VERSION = "1.15.0.dev1"
VERSION = "".join(re.findall(r"(\d+\.)(\d+\.)(\d+)", DEV_VERSION)[0])


def is_release_version():
    return bool(re.match(r"^\d+\.\d+\.\d+$", VERSION))
