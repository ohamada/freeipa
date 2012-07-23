# Authors: Karl MacMillan <kmacmillan@mentalrootkit.com>
#
# Copyright (C) 2007  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import sys
import os, socket
import tempfile
import pwd
from ipapython import sysrestore
from ipapython import ipautil
from ipapython import services as ipaservices
from ipalib import errors
import ldap
from ipaserver import ipaldap
import base64
import time
import datetime
from ipaserver.install import installutils
from ipapython.ipa_log_manager import *

CACERT = "/etc/ipa/ca.crt"

# The service name as stored in cn=masters,cn=ipa,cn=etc. In the tuple
# the first value is the *nix service name, the second the start order.
SERVICE_LIST = {
    'KDC':('krb5kdc', 10),
    'KPASSWD':('kadmin', 20),
    'DNS':('named', 30),
    'MEMCACHE':('ipa_memcached', 39),
    'HTTP':('httpd', 40),
    'CA':('pki-cad', 50),
    'ADTRUST':('smb', 60),
    'EXTID':('winbind', 70)
}

def print_msg(message, output_fd=sys.stdout):
    root_logger.debug(message)
    output_fd.write(message)
    output_fd.write("\n")


class Service(object):
    def __init__(self, service_name, sstore=None, dm_password=None, ldapi=False):
        self.service_name = service_name
        self.service = ipaservices.service(service_name)
        self.steps = []
        self.output_fd = sys.stdout
        self.dm_password = dm_password
        self.ldapi = ldapi

        self.fqdn = socket.gethostname()
        self.admin_conn = None

        if sstore:
            self.sstore = sstore
        else:
            self.sstore = sysrestore.StateFile('/var/lib/ipa/sysrestore')

        self.realm = None
        self.suffix = None
        self.principal = None
        self.dercert = None

    def ldap_connect(self):
        if self.ldapi:
            if not self.realm:
                raise RuntimeError('realm must be set to use ldapi connection')
            self.admin_conn = self.__get_conn(None, None, ldapi=True, realm=self.realm)
        else:
            self.admin_conn = self.__get_conn(self.fqdn, self.dm_password)

    def ldap_disconnect(self):
        self.admin_conn.unbind()
        self.admin_conn = None

    def _ldap_mod(self, ldif, sub_dict = None, user_cn = None, user_pw = None):

        pw_name = None
        fd = None
        path = ipautil.SHARE_DIR + ldif
        hostname = installutils.get_fqdn()
        nologlist=[]

        if sub_dict is not None:
            txt = ipautil.template_file(path, sub_dict)
            fd = ipautil.write_tmp_file(txt)
            path = fd.name

            # do not log passwords
            if sub_dict.has_key('PASSWORD'):
                nologlist.append(sub_dict['PASSWORD'])
            if sub_dict.has_key('RANDOM_PASSWORD'):
                nologlist.append(sub_dict['RANDOM_PASSWORD'])

        if self.dm_password or (user_cn and user_pw):
            [pw_fd, pw_name] = tempfile.mkstemp()
            if user_cn:
                cn = "cn=%s" % user_cn
                os.write(pw_fd, user_pw)
            else:
                cn = "cn=Directory Manager"
                os.write(pw_fd, self.dm_password)
            os.close(pw_fd)
            auth_parms = ["-x", "-D", cn, "-y", pw_name]
        else:
            auth_parms = ["-Y", "GSSAPI"]

        args = ["/usr/bin/ldapmodify", "-h", hostname, "-v", "-f", path]
        args += auth_parms

        try:
            try:
                ipautil.run(args, nolog=nologlist)
            except ipautil.CalledProcessError, e:
                root_logger.critical("Failed to load %s: %s" % (ldif, str(e)))
        finally:
            if pw_name:
                os.remove(pw_name)

        if fd is not None:
            fd.close()

    def move_service(self, principal):
        """
        Used to move a principal entry created by kadmin.local from
        cn=kerberos to cn=services
        """

        dn = "krbprincipalname=%s,cn=%s,cn=kerberos,%s" % (principal, self.realm, self.suffix)
        try:
            entry = self.admin_conn.getEntry(dn, ldap.SCOPE_BASE)
        except errors.NotFound:
            # There is no service in the wrong location, nothing to do.
            # This can happen when installing a replica
            return
        newdn = "krbprincipalname=%s,cn=services,cn=accounts,%s" % (principal, self.suffix)
        hostdn = "fqdn=%s,cn=computers,cn=accounts,%s" % (self.fqdn, self.suffix)
        self.admin_conn.deleteEntry(dn)
        entry.dn = newdn
        classes = entry.getValues("objectclass")
        classes = classes + ["ipaobject", "ipaservice", "pkiuser"]
        entry.setValues("objectclass", list(set(classes)))
        entry.setValue("ipauniqueid", 'autogenerate')
        entry.setValue("managedby", hostdn)
        self.admin_conn.addEntry(entry)
        return newdn

    def add_simple_service(self, principal):
        """
        Add a very basic IPA service.

        The principal needs to be fully-formed: service/host@REALM
        """
        if not self.admin_conn:
            self.ldap_connect()

        dn = "krbprincipalname=%s,cn=services,cn=accounts,%s" % (principal, self.suffix)
        hostdn = "fqdn=%s,cn=computers,cn=accounts,%s" % (self.fqdn, self.suffix)
        entry = ipaldap.Entry(dn)
        entry.setValues("objectclass", ["krbprincipal", "krbprincipalaux", "krbticketpolicyaux", "ipaobject", "ipaservice", "pkiuser"])
        entry.setValue("krbprincipalname", principal)
        entry.setValue("ipauniqueid", 'autogenerate')
        entry.setValue("managedby", hostdn)
        self.admin_conn.addEntry(entry)
        return dn

    def add_cert_to_service(self):
        """
        Add a certificate to a service

        This server cert should be in DER format.
        """

        if not self.admin_conn:
            self.ldap_connect()

        dn = "krbprincipalname=%s,cn=services,cn=accounts,%s" % (self.principal, self.suffix)
        mod = [(ldap.MOD_ADD, 'userCertificate', self.dercert)]
        try:
            self.admin_conn.modify_s(dn, mod)
        except Exception, e:
            root_logger.critical("Could not add certificate to service %s entry: %s" % (self.principal, str(e)))

    def is_configured(self):
        return self.sstore.has_state(self.service_name)

    def set_output(self, fd):
        self.output_fd = fd

    def stop(self, instance_name="", capture_output=True):
        self.service.stop(instance_name, capture_output=capture_output)

    def start(self, instance_name="", capture_output=True, wait=True):
        self.service.start(instance_name, capture_output=capture_output, wait=wait)

    def restart(self, instance_name="", capture_output=True, wait=True):
        self.service.restart(instance_name, capture_output=capture_output, wait=wait)

    def is_running(self):
        return self.service.is_running()

    def install(self):
        self.service.install()

    def remove(self):
        self.service.remove()

    def enable(self):
        self.service.enable()

    def disable(self):
        self.service.disable()

    def is_enabled(self):
        return self.service.is_enabled()

    def backup_state(self, key, value):
        self.sstore.backup_state(self.service_name, key, value)

    def restore_state(self, key):
        return self.sstore.restore_state(self.service_name, key)

    def print_msg(self, message):
        print_msg(message, self.output_fd)

    def step(self, message, method):
        self.steps.append((message, method))

    def start_creation(self, message, runtime=-1):
        if runtime > 0:
            plural=''
            est = time.localtime(runtime)
            if est.tm_min > 0:
                if est.tm_min > 1:
                    plural = 's'
                if est.tm_sec > 0:
                    self.print_msg('%s: Estimated time %d minute%s %d seconds' % (message, est.tm_min, plural, est.tm_sec))
                else:
                    self.print_msg('%s: Estimated time %d minute%s' % (message, est.tm_min, plural))
            else:
                if est.tm_sec > 1:
                    plural = 's'
                self.print_msg('%s: Estimated time %d second%s' % (message, est.tm_sec, plural))
        else:
            self.print_msg(message)

        step = 0
        for (message, method) in self.steps:
            self.print_msg("  [%d/%d]: %s" % (step+1, len(self.steps), message))
            s = datetime.datetime.now()
            method()
            e = datetime.datetime.now()
            d = e - s
            root_logger.debug("  duration: %d seconds" % d.seconds)
            step += 1

        self.print_msg("done configuring %s." % self.service_name)

        self.steps = []

    def __get_conn(self, fqdn, dm_password, ldapi=False, realm=None):
        # If we are passed a password we'll use it as the DM password
        # otherwise we'll do a GSSAPI bind.
        try:
#            conn = ipaldap.IPAdmin(fqdn, port=636, cacert=CACERT)
            if ldapi:
                conn = ipaldap.IPAdmin(ldapi=ldapi, realm=realm)
            else:
                conn = ipaldap.IPAdmin(fqdn, port=389)
            if dm_password:
                conn.do_simple_bind(bindpw=dm_password)
            elif os.getegid() == 0 and self.ldapi:
                try:
                    # autobind
                    pw_name = pwd.getpwuid(os.geteuid()).pw_name
                    conn.do_external_bind(pw_name)
                except errors.NotFound:
                    # Fall back
                    conn.do_sasl_gssapi_bind()
            else:
                conn.do_sasl_gssapi_bind()
        except Exception, e:
            root_logger.debug("Could not connect to the Directory Server on %s: %s" % (fqdn, str(e)))
            raise e

        return conn

    def ldap_enable(self, name, fqdn, dm_password, ldap_suffix):
        self.disable()
        if not self.admin_conn:
            self.ldap_connect()

        entry_name = "cn=%s,cn=%s,%s,%s" % (name, fqdn,
                                            "cn=masters,cn=ipa,cn=etc",
                                            ldap_suffix)
        order = SERVICE_LIST[name][1]
        entry = ipaldap.Entry(entry_name)
        entry.setValues("objectclass",
                        "nsContainer", "ipaConfigObject")
        entry.setValues("cn", name)
        entry.setValues("ipaconfigstring",
                        "enabledService", "startOrder " + str(order))

        try:
            self.admin_conn.addEntry(entry)
        except (ldap.ALREADY_EXISTS, errors.DuplicateEntry), e:
            root_logger.debug("failed to add %s Service startup entry" % name)
            raise e

class SimpleServiceInstance(Service):
    def create_instance(self, gensvc_name=None, fqdn=None, dm_password=None, ldap_suffix=None):
        self.gensvc_name = gensvc_name
        self.fqdn = fqdn
        self.dm_password = dm_password
        self.suffix = ldap_suffix

        self.step("starting %s " % self.service_name, self.__start)
        self.step("configuring %s to start on boot" % self.service_name, self.__enable)
        self.start_creation("Configuring %s" % self.service_name)

    def __start(self):
        self.backup_state("running", self.is_running())
        self.restart()

    def __enable(self):
        self.enable()
        self.backup_state("enabled", self.is_enabled())
        if self.gensvc_name == None:
            self.enable()
        else:
            self.ldap_enable(self.gensvc_name, self.fqdn,
                             self.dm_password, self.suffix)

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring %s" % self.service_name)

        running = self.restore_state("running")
        enabled = not self.restore_state("enabled")

        if not running is None and not running:
            self.stop()
        if not enabled is None and not enabled:
            self.disable()
            self.remove()
