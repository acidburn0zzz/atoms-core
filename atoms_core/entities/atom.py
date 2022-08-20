# atom.py
#
# Copyright 2022 mirkobrombin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundationat version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import uuid
import shutil
import orjson
import tempfile
import datetime
import importlib

from atoms_core.exceptions.atom import AtomsWrongAtomData, AtomsConfigFileNotFound
from atoms_core.exceptions.download import AtomsHashMissmatchError
from atoms_core.exceptions.image import AtomsFailToDownloadImage
from atoms_core.exceptions.distribution import AtomsUnreachableRemote, AtomsMisconfiguredDistribution
from atoms_core.exceptions.podman import AtomsFailToCreateContainer
from atoms_core.utils.paths import AtomsPathsUtils
from atoms_core.utils.image import AtomsImageUtils
from atoms_core.utils.distribution import AtomsDistributionsUtils
from atoms_core.utils.command import CommandUtils
from atoms_core.utils.file import FileUtils
from atoms_core.wrappers.proot import ProotWrapper
from atoms_core.wrappers.servicectl import ServicectlWrapper
from atoms_core.wrappers.distrobox import DistroboxWrapper
from atoms_core.entities.distributions.host import Host


class Atom:
    name: str
    distribution_id: str
    creation_date: str
    upate_date: str
    relative_path: str

    def __init__(
        self,
        instance: "AtomsInstance",
        name: str,
        distribution_id: str = None,
        relative_path: str = None,
        creation_date: str = None,
        update_date: str = None,
        container_id: str = None,
        container_image: str = None,
        system_shell: bool = False,
        bind_themes: bool = False,
        bind_icons: bool = False,
        bind_fonts: bool = False,
        bind_extra_mounts: list = None,
    ):
        if update_date is None and (container_id or system_shell):
            update_date = datetime.datetime.now().isoformat()
        elif update_date is None:
            update_date = creation_date

        self._instance = instance
        self.name = name.strip()
        self.distribution_id = distribution_id
        self.relative_path = relative_path
        self.creation_date = creation_date
        self.update_date = update_date
        self.container_id = container_id
        self.container_image = container_image
        self.__system_shell = system_shell
        self.bind_themes = bind_themes
        self.bind_icons = bind_icons
        self.bind_fonts = bind_fonts
        self.bind_extra_mounts = bind_extra_mounts or []
        self.__proot_wrapper = ProotWrapper()

        if container_id:
            self.__distrobox_wrapper = DistroboxWrapper()
        
    @staticmethod
    def get_extra_default_options():
        return {
            "bindThemes": False,
            "bindIcons": False,
            "bindFonts": False,
            "bindExtraMounts": []
        }

    @classmethod
    def from_dict(cls, instance: "AtomsInstance", data: dict) -> "Atom":
        if None in [
            data.get("name"),
            data.get("distributionId"),
            data.get("creationDate"),
            data.get("updateDate"),
            data.get("relativePath")
        ]:
            raise AtomsWrongAtomData(data)

        for key, val in Atom.get_extra_default_options().items():
            if key not in data:
                data[key] = val

        return cls(
            instance,
            data['name'],
            data['distributionId'],
            data['relativePath'],
            data['creationDate'],
            data['updateDate'],
            bind_themes=data['bindThemes'],
            bind_icons=data['bindIcons'],
            bind_fonts=data['bindFonts'],
            bind_extra_mounts=data['bindExtraMounts']
        )

    @classmethod
    def load(cls, instance: "AtomsInstance", relative_path: str) -> "Atom":
        path = os.path.join(AtomsPathsUtils.get_atom_path(
            instance.config, relative_path), "atom.json")
        try:
            with open(path, "r") as f:
                data = orjson.loads(f.read())
        except FileNotFoundError:
            raise AtomsConfigFileNotFound(path)
        return cls.from_dict(instance, data)

    @classmethod
    def load_from_container(
        cls,
        instance: "AtomsInstance",
        creation_date: str,
        container_name: str,
        container_image: str,
        container_id: str
    ) -> "Atom":
        return cls(
            instance,
            container_name,
            creation_date=creation_date,
            container_id=container_id,
            container_image=container_image
        )

    @classmethod
    def new(
        cls,
        instance: 'AtomsInstance',
        name: str,
        distribution: 'AtomDistribution',
        architecture: str,
        release: str,
        download_fn: callable = None,
        config_fn: callable = None,
        unpack_fn: callable = None,
        finalizing_fn: callable = None,
        error_fn: callable = None
    ) -> 'Atom':
        # Get image
        try:
            image = AtomsImageUtils.get_image(
                instance, distribution, architecture, release, download_fn)
        except AtomsHashMissmatchError:
            if error_fn:
                instance.client_bridge.exec_on_main(error_fn, "Hash missmatch.")
            return
        except AtomsFailToDownloadImage:
            if error_fn:
                instance.client_bridge.exec_on_main(
                    error_fn, "Fail to download image, it might be a temporary problem.")
            return
        except AtomsUnreachableRemote:
            if error_fn:
                instance.client_bridge.exec_on_main(
                    error_fn, "Unreachable remote, it might be a temporary problem.")
            return
        except AtomsMisconfiguredDistribution as e:
            if error_fn:
                instance.client_bridge.exec_on_main(error_fn, str(e))
            return

        # Create configuration
        if config_fn:
            instance.client_bridge.exec_on_main(config_fn, 0)

        date = datetime.datetime.now().isoformat()
        relative_path = str(uuid.uuid4()) + ".atom"
        atom_path = AtomsPathsUtils.get_atom_path(instance.config, relative_path)
        chroot_path = os.path.join(atom_path, "chroot")
        root_path = os.path.join(chroot_path, "root")
        atom = cls(
            instance, name, distribution.distribution_id,
            relative_path, date,
            bind_themes=False, 
            bind_icons=False, 
            bind_fonts=False, 
            bind_extra_mounts=[]
        )
        os.makedirs(chroot_path)

        if config_fn:
            instance.client_bridge.exec_on_main(config_fn, 1)

        # Unpack image
        if unpack_fn:
            instance.client_bridge.exec_on_main(unpack_fn, 0)

        image.unpack(chroot_path)
        os.makedirs(root_path, exist_ok=True)

        if unpack_fn:
            instance.client_bridge.exec_on_main(unpack_fn, 1)

        # Finalize and distro specific workarounds
        # install servicectl to be able to manage services in the chroot
        ServicectlWrapper().install_to_path(os.path.join(atom.fs_path, "usr/local/bin"))

        if finalizing_fn:
            instance.client_bridge.exec_on_main(finalizing_fn, 0)

        # run post unpack if any
        distribution.post_unpack(chroot_path)

        # workaround for unsigned repo in ubuntu (need to investigate the cause)
        # TODO: move to post_unpack
        if distribution.distribution_id == "ubuntu":
            with open(os.path.join(chroot_path, "etc/apt/sources.list"), "r") as f:
                sources = f.read()
            sources = sources.replace("deb ", "deb [trusted=yes] ")
            sources = sources.replace("deb-src ", "deb-src [trusted=yes] ")
            with open(os.path.join(chroot_path, "etc/apt/sources.list"), "w") as f:
                f.write(sources)
        atom.save()
        
        if finalizing_fn:
            instance.client_bridge.exec_on_main(finalizing_fn, 1)

        return atom

    @classmethod
    def new_container(
        cls,
        instance: 'AtomsInstance',
        name: str,
        container_image: str,
        distrobox_fn: callable = None,
        finalizing_fn: callable = None,
        error_fn: callable = None
    ) -> 'Atom':
        # Distrobox container creation
        if distrobox_fn:
            instance.client_bridge.exec_on_main(distrobox_fn, 0)

        try:
            container_id = DistroboxWrapper().new_container(name, container_image)
        except AtomsFailToCreateContainer:
            if error_fn:
                instance.client_bridge.exec_on_main(
                    error_fn, "Fail to create container, it might be a temporary problem or a wrong image was requested.")
            return

        if distrobox_fn:
            instance.client_bridge.exec_on_main(distrobox_fn, 1)

        # Finalizing
        if finalizing_fn:
            instance.client_bridge.exec_on_main(finalizing_fn, 0)

        atom = cls.load_from_container(
            instance,
            datetime.datetime.now().isoformat(),
            name,
            container_image,
            container_id
        )

        if finalizing_fn:
            instance.client_bridge.exec_on_main(finalizing_fn, 1)

        return atom
    
    @classmethod
    def new_system_shell(cls, instance: 'AtomsInstance'):
        return cls(instance, "system-shell", system_shell=True)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "distributionId": self.distribution_id,
            "relativePath": self.relative_path,
            "creationDate": self.creation_date,
            "updateDate": self.update_date,
            "bindThemes": self.bind_themes,
            "bindIcons": self.bind_icons,
            "bindFonts": self.bind_fonts,
            "bindExtraMounts": self.bind_extra_mounts
        }

    def save(self):
        if self.is_distrobox_container or self.__system_shell:
            raise AtomsCannotSavePodmanContainers()

        path = os.path.join(self.path, "atom.json")
        with open(path, "wb") as f:
            f.write(orjson.dumps(self.to_dict(), f,
                    option=orjson.OPT_NON_STR_KEYS))

    def generate_command(self, command: list, environment: list = None, track_exit: bool = True) -> tuple:
        if self.is_distrobox_container:
            command, environment, working_directory = self.__generate_distrobox_command(
                command, environment)
        elif self.__system_shell:
            command, environment, working_directory = self.__generate_system_shell_command()
        else:
            command, environment, working_directory = self.__generate_proot_command(
                command, environment)

        if track_exit:
            command = ["sh", self.__get_launcher_script()] + command

        return command, environment, working_directory

    def __generate_proot_command(self, command: list, environment: list = None) -> tuple:
        if environment is None:
            environment = []

        _command = self.__proot_wrapper.get_proot_command_for_chroot(
            self.fs_path, command, bind_mounts=self.bind_mounts
        )
        return _command, environment, self.root_path

    def __generate_distrobox_command(self, command: list, environment: list = None) -> tuple:
        if environment is None:
            environment = []

        _command = self.__distrobox_wrapper.get_distrobox_command_for_container(
            self.container_id, command)
        return _command, environment, self.root_path
    
    def __generate_system_shell_command(self) -> tuple:
        _command = [os.environ["SHELL"]]
        return _command, [], "/"

    def __get_launcher_script(self) -> str:
        script = """#!/bin/bash
while true; do
    clear
    $@
    read -n 1 -s -r -p "Press any [Key] to restart the Atom Console…";
done
"""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as f:
            f.write(script)
            return f.name

    def destroy(self):
        if self.is_distrobox_container or self.__system_shell:
            self.__distrobox_wrapper.destroy_container(self.container_id, self.name)
            return

        # NOTE: might not be the best way to do this but shutil raises an
        #       error if has no permissions to remove the directory since
        #       the homedir is mounted in some way (not unmoutable).
        #       A better way would be stop the running proot process and
        #       then remove the directory, but since Atoms has a no track
        #       of the proot process, this is the best we can do for now.
        binary_path = shutil.which("rm")
        FileUtils.native_rm(self.path)

    def kill(self):
        if self.is_distrobox_container or self.__system_shell:
            self.__distrobox_wrapper.stop_container(self.container_id)
            return

        pids = ProcUtils.find_proc_by_cmdline(self.relative_path)
        for pid in pids:
            pid.kill()

    def rename(self, new_name: str):
        if self.is_distrobox_container or self.__system_shell:
            raise AtomsCannotRenamePodmanContainers()
        self.name = new_name
        self.save()

    def stop_distrobox_container(self):
        self.__distrobox_wrapper.stop_container(self.container_id)
    
    def set_bind_themes(self, status: bool):
        self.bind_themes = status
        self.save()

    def set_bind_icons(self, status: bool):
        self.bind_icons = status
        self.save()

    def set_bind_fonts(self, status: bool):
        self.bind_fonts = status
        self.save()

    @property
    def path(self) -> str:
        if self.is_distrobox_container or self.__system_shell:
            return ""
        return AtomsPathsUtils.get_atom_path(self._instance.config, self.relative_path)

    @property
    def fs_path(self) -> str:
        if self.is_distrobox_container or self.__system_shell:
            return ""
        return os.path.join(
            AtomsPathsUtils.get_atom_path(
                self._instance.config, self.relative_path),
            "chroot"
        )

    @property
    def root_path(self) -> str:
        if self.is_distrobox_container or self.__system_shell:
            return ""
        return os.path.join(self.fs_path, "root")

    @property
    def distribution(self) -> 'AtomDistribution':
        if self.is_distrobox_container:
            return AtomsDistributionsUtils.get_distribution_by_container_image(self.container_image)
        if self.__system_shell:
            return Host()
        return AtomsDistributionsUtils.get_distribution(self.distribution_id)

    @property
    def enter_command(self) -> list:
        return self.generate_command([])
    
    @property
    def untracked_enter_command(self) -> list:
        return self.generate_command([], track_exit=False)

    @property
    def formatted_update_date(self) -> str:
        return datetime.datetime.strptime(
            self.update_date, "%Y-%m-%dT%H:%M:%S.%f"
        ).strftime("%d %B, %Y %H:%M:%S")

    @property
    def is_distrobox_container(self) -> bool:
        return self.container_id is not None

    @property
    def is_system_shell(self) -> bool:
        return self.__system_shell
    
    @property
    def aid(self) -> str:
        if self.is_distrobox_container or self.__system_shell:
            return self.container_id
        return self.relative_path

    @property
    def bind_mounts(self) -> list:
        mounts = []
        if self.bind_themes:
            mounts.append(("/usr/share/themes", "/usr/share/themes"))
        if self.bind_icons:
            mounts.append(("/usr/share/icons", "/usr/share/icons"))
        if self.bind_fonts:
            mounts.append(("/usr/share/fonts", "/usr/share/fonts"))
        if self.bind_extra_mounts:
            mounts += self.bind_extra_mounts
        return mounts

    def __str__(self):
        if self.is_distrobox_container:
            return f"Atom {self.name} (distrobox)"
        elif self.is_system_shell:
            return f"Atom {self.name} (system shell)"
        return f"Atom: {self.name}"
