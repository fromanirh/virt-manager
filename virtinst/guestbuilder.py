#
# Builder helpers for all guests
#
# Copyright 2018 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.


import re


from . import DeviceDisk
from . import (PXEInstaller, DistroInstaller, ContainerInstaller,
               ImportInstaller)


def _get_guest(log, conn, options, missing_features):
    # Set up all virt/hypervisor parameters
    if sum([bool(f) for f in [options.fullvirt,
                              options.paravirt,
                              options.container]]) > 1:
        fail(_("Can't do more than one of --hvm, --paravirt, or --container"))

    req_hv_type = options.hv_type and options.hv_type.lower() or None
    if options.fullvirt:
        req_virt_type = "hvm"
    elif options.paravirt:
        req_virt_type = "xen"
    elif options.container:
        req_virt_type = "exe"
    else:
        # This should force capabilities to give us the most sensible default
        req_virt_type = None

    log.debug("Requesting virt method '%s', hv type '%s'.",
                  (req_virt_type and req_virt_type or _("default")),
                  (req_hv_type and req_hv_type or _("default")))

    arch = options.arch
    if re.match("i.86", arch or ""):
        arch = "i686"

    try:
        guest = conn.caps.lookup_virtinst_guest(
            os_type=req_virt_type,
            arch=arch,
            typ=req_hv_type,
            machine=options.machine)
    except Exception as e:
        fail(e)

    if (not req_virt_type and
        not req_hv_type and
        conn.is_qemu() and
        guest.os.arch in ["i686", "x86_64"] and
        not guest.type == "kvm"):
        log.warning("KVM acceleration not available, using '%s'",
                     guest.type)
        missing_features.append("KVM")

    return guest


class Builder(object):

    def __init__(self, log, conn, options):
        self._log = log
        self._conn = conn
        self._options = options
        # features requested, but not available.
        self._missing_features = []

    def postprocess_options(self, options):
        # to be reimplemented in inherited classes
        pass

    def validate(self, options, guest):
        # to be reimplemented in inherited classes
        pass

    def build_instance(self):
        guest = _get_guest(self._log, self._conn, self._options,
                self._missing_features)

        self._log.debug("Received virt method '%s'", guest.type)
        self._log.debug("Hypervisor name is '%s'", guest.os.os_type)

        guest.installer = self.build_installer(guest.os.os_type)

        self.postprocess_options(self._options)

        # non-xml install options
        guest.installer.extraargs = self._options.extra_args
        guest.installer.initrd_injections = self._options.initrd_inject
        guest.autostart = self._options.autostart

        if self._options.name:
            guest.name = self._options.name
        if self._options.uuid:
            guest.uuid = self._options.uuid
        if self._options.description:
            guest.description = self._options.description

        self.validate(self._options, guest)

        _set_install_media(guest, self._options.location, self._options.cdrom,
           self._options.distro_variant)

        guest.add_default_devices()

        _check_uefi_aarch64(self._log, guest, self._missing_features)
        _check_smm(guest)

        # Various little validations about option collisions. Need to do
        # this after setting guest.installer at least
        _check_option_collisions(self._options, guest)

        return guest, self._missing_features.copy()

    def build_installer(self, virt_type):
        # Build the Installer instance
        if self._options.pxe:
            instclass = PXEInstaller
        elif self._options.cdrom or self._options.location or self._options.livecd:
            instclass = DistroInstaller
        elif virt_type == "exe":
            instclass = ContainerInstaller
        elif self._options.import_install or self._options.boot:
            if self._options.import_install and self._options.nodisks:
                # TODO: raise
                fail(_("A disk device must be specified with --import."))
            self._options.import_install = True
            instclass = ImportInstaller
        elif self._options.xmlonly:
            instclass = ImportInstaller
        else:
            instclass = DistroInstaller

        installer = instclass(self._conn)
        if self._options.livecd:
            installer.livecd = True

        return installer


def get_xml(guest):
    start_xml, final_xml = guest.start_install(dry=False, return_xml=True)
    if not start_xml:
        start_xml = final_xml
        final_xml = ""
    return start_xml + final_xml


def install_specified(location, cdpath, pxe, import_install):
    return bool(pxe or cdpath or location or import_install)


def cdrom_specified(guest, disk=None):
    disks = guest.devices.disk

    for d in disks:
        if d.device == DeviceDisk.DEVICE_CDROM:
            return True

    # Probably haven't set up disks yet
    if not disks and disk:
        for opts in disk:
            if opts.count("device=cdrom"):
                return True

    return False


def _check_uefi_aarch64(log, guest, missing_features):
    # Default to UEFI for aarch64
    if (guest.os.is_arm64() and
        not guest.os.kernel and
        not guest.os.loader and
        guest.os.loader_ro is None and
        guest.os.nvram is None):
        try:
            guest.set_uefi_default()
        except Exception as e:
            log.debug("Error setting UEFI default for aarch64",
                exc_info=True)
            log.warning("Couldn't configure UEFI: %s", e)
            log.warning("Your aarch64 VM may not boot successfully.")
            missing_features.append("UEFI")


def _check_smm(guest):
    # TODO: raise, not fail() directly
    # Check usability of SMM feature
    if guest.features.smm:
        if not guest.os.is_x86():
            fail(_("SMM feature is valid only for x86 architecture."))

        if guest.os.machine is None:
            guest.os.machine = "q35"
        elif not guest.os.is_q35():
            fail(_("SMM feature is valid only for q35 machine type"))


def _set_install_media(guest, location, cdpath, distro_variant):
    try:
        cdinstall = bool(not location and (cdpath or cdrom_specified(guest)))

        if cdinstall or cdpath:
            guest.installer.cdrom = True
        if location or cdpath:
            guest.installer.location = (location or cdpath)

        if distro_variant not in ["auto", "none"]:
            guest.os_variant = distro_variant

        guest.installer.check_location(guest)

        if distro_variant == "auto":
            guest.os_variant = guest.installer.detect_distro(guest)
    except ValueError as e:
        # TODO: raise not fail
        fail(_("Error validating install location: %s") % str(e))


# TODO: raise not fail
def _check_option_collisions(options, guest):
    if options.noreboot and options.transient:
        fail(_("--noreboot and --transient can not be specified together"))

    # Install collisions
    if sum([bool(l) for l in [options.pxe, options.location,
                      options.cdrom, options.import_install]]) > 1:
        fail(_("Only one install method can be used (%(methods)s)") %
             {"methods": install_methods})

    if (guest.os.is_container() and
        install_specified(options.location, options.cdrom,
                          options.pxe, options.import_install)):
        fail(_("Install methods (%s) cannot be specified for "
               "container guests") % install_methods)

    if guest.os.is_xenpv():
        if options.pxe:
            fail(_("Network PXE boot is not supported for paravirtualized "
                   "guests"))
        if options.cdrom or options.livecd:
            fail(_("Paravirtualized guests cannot install off cdrom media."))

    if (options.location and
        guest.conn.is_remote() and not
        guest.conn.support_remote_url_install()):
        fail(_("Libvirt version does not support remote --location installs"))

    if not options.location and options.extra_args:
        fail(_("--extra-args only work if specified with --location."))
    if not options.location and options.initrd_inject:
        fail(_("--initrd-inject only works if specified with --location."))
             cdrom_err)

