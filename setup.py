# -*- coding: utf-8 -*-
import os

from setuptools import setup

try:
    from octoprint_setuptools import get_babel_commandclasses, CleanCommand

    plugin_identifier = "grblpcb"
    plugin_package = "octoprint_grblpcb"

    translation_dir = os.path.join(".", "translations")
    pot_file = os.path.join(translation_dir, "messages.pot")
    bundled_dir = os.path.join(".", plugin_package, "translations")

    cmdclass = get_babel_commandclasses(
        pot_file=pot_file,
        output_dir=translation_dir,
        bundled_dir=bundled_dir,
        pack_name_prefix="OctoPrint-GrblPCB-i18n-",
        pack_path_prefix=f"_plugins/{plugin_identifier}/",
    )
    cmdclass["clean"] = CleanCommand.for_options(source_folder=plugin_package)
except ImportError:
    cmdclass = {}

setup(cmdclass=cmdclass)
