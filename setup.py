# -*- coding: utf-8 -*-

import sys

import setuptools

if sys.version_info < (3, 9, 0):
    sys.exit("The pydanfossally module requires Python 3.9.0 or later")

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="pydanfossally",
    description="Danfoss Ally API library",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Malene Trab",
    author_email="malene@trab.dk",
    license="MIT",
    url="https://github.com/mtrab/pydanfossally",
    packages=setuptools.find_packages(),
    project_urls={
        "Bug Tracker": "https://github.com/mtrab/pydanfossally/issues",
    },
    install_requires=["requests>=2.28.0"],
    version="0.0.30",
)
