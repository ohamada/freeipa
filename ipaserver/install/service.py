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
import time
import datetime
import re

from ipapython import sysrestore, ipautil, dogtag, ipaldap
from ipapython import services as ipaservices
from ipapython.dn import DN
from ipapython.ipa_log_manager import *
from ipalib import errors

CACERT = "/etc/ipa/ca.crt"

forward_entry = re.compile("^dn: .*cn=config$")

# Autobind modes
AUTO = 1
ENABLED = 2
DISABLED = 3

# The service name as stored in cn=masters,cn=ipa,cn=etc. In the tuple
# the first value is the *nix service name, the second the start order.
SERVICE_LIST = {
    'KDC':('krb5kdc', 10),
    'KPASSWD':('kadmin', 20),
    'DNS':('named', 30),
    'MEMCACHE':('ipa_memcached', 39),
    'HTTP':('httpd', 40),
    'CA':('%sd' % dogtag.configured_constants().PKI_INSTANCE_NAME, 50),
    'ADTRUST':('smb', 60),
    'EXTID':('winbind', 70)
}

# Maximal number of hops allowed for referral following
REFERRAL_CNT = 10

LDAP_ERR_REFERRAL = 10

def print_msg(message, output_fd=sys.stdout):
    root_logger.debug(message)
    output_fd.write(message)
    output_fd.write("\n")

def is_forward_mod(path):
    with open(path,'r') as f:
        for line in f.readlines():
            res = forward_entry.match(line)
            if res is not None:
                return False
    return True


class Service(object):
    def __init__(self, service_name, service_desc=None, sstore=None, dm_password=None, ldapi=True, autobind=AUTO, on_master=True, master_fqdn=None):
        self.service_name = service_name
        self.service_desc = service_desc
        self.service = ipaservices.service(service_name)
        self.steps = []
        self.output_fd = sys.stdout
        self.dm_password = dm_password
        self.ldapi = ldapi
        self.autobind = autobind
        
        self.on_master = True
        self.master_fqdn = None
        
        self.fqdn = socket.gethostname()
        self.admin_conn = None
        
        self.master_conn = None

        if sstore:
            self.sstore = sstore
        else:
            self.sstore = sysrestore.StateFile('/var/lib/ipa/sysrestore')

        self.realm = None
        self.suffix = DN()
        self.principal = None
        self.dercert = None

        self.replica_type = None
        self.farm_fqdn = None

    def _set_service_location(self, server_type="master"):
        if server_type != "master":
            self.on_master = False
        if not self.on_master:
            if self.master_fqdn is None:
                raise errors.NotFound(reason="missing master fqdn")
            if self.dm_password is None:
                raise errors.NotFound(reason="missing Directory Manager password for master")
            if self.farm_fqdn is None:
                raise errors.NotFound(reason="missing farm server hostname")

    def ldap_connect(self):
        # If DM password is provided, we use it
        # If autobind was requested, attempt autobind when root and ldapi
        # If autobind was disabled or not succeeded, go with GSSAPI
        # LDAPI can be used with either autobind or GSSAPI
        # LDAPI requires realm to be set
        try:
            if self.ldapi:
                if not self.realm:
                    raise errors.NotFound(reason="realm is missing for %s" % (self))
                conn = ipaldap.IPAdmin(ldapi=self.ldapi, realm=self.realm)
            else:
                conn = ipaldap.IPAdmin(self.fqdn, port=389)
            if self.dm_password:
                conn.do_simple_bind(bindpw=self.dm_password)
            elif self.autobind in [AUTO, ENABLED]:
                if os.getegid() == 0 and self.ldapi:
                    try:
                        # autobind
                        pw_name = pwd.getpwuid(os.geteuid()).pw_name
                        conn.do_external_bind(pw_name)
                    except errors.NotFound, e:
                        if self.autobind == AUTO:
                            # Fall back
                            conn.do_sasl_gssapi_bind()
                        else:
                            # autobind was required and failed, raise
                            # exception that it failed
                            raise e
                else:
                    conn.do_sasl_gssapi_bind()
            else:
                conn.do_sasl_gssapi_bind()
        except Exception, e:
            root_logger.debug("Could not connect to the Directory Server on %s: %s" % (self.fqdn, str(e)))
            raise

        self.admin_conn = conn
        
        if not self.on_master:
            try:
                master_conn = ipaldap.IPAdmin(self.master_fqdn, port=389)
                master_conn.do_simple_bind(bindpw=self.dm_password)
            except Exception, e:
                root_logger.debug("Could not connect to the Directory Server on %s: %s" % (self.master_fqdn, str(e)))
                raise
    
            self.master_conn = master_conn
                

    def ldap_disconnect(self):
        self.admin_conn.unbind()
        self.admin_conn = None
        if not self.master_conn is None:
            self.master_conn.unbind()
            self.master_conn = None

    def _ldap_mod(self, ldif, sub_dict = None):

        pw_name = None
        fd = None
        path = ipautil.SHARE_DIR + ldif
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

        temp_args = ["/usr/bin/ldapmodify", "-v", "-f", path]

        # As we always connect to the local host,
        # use URI of admin connection
        if not self.admin_conn:
            self.ldap_connect()
        if (not self.on_master) and is_forward_mod(path):
            arg_conn_uri = ["-H", self.master_conn.ldap_uri]
        else:
            arg_conn_uri = ["-H", self.admin_conn.ldap_uri]

        auth_parms = []
        if self.dm_password:
            [pw_fd, pw_name] = tempfile.mkstemp()
            os.write(pw_fd, self.dm_password)
            os.close(pw_fd)
            auth_parms = ["-x", "-D", "cn=Directory Manager", "-y", pw_name]
        else:
            # always try GSSAPI auth when not using DM password or not being root
            if os.getegid() != 0:
                auth_parms = ["-Y", "GSSAPI"]

        temp_args += auth_parms
        args = temp_args + arg_conn_uri

        try:
            (stdo,stde,errc) = ipautil.run(args, raiseonerr=False, nolog=nologlist)
            if not self.on_master:
                # referral returned
                if errc == LDAP_ERR_REFERRAL:
                    ref_cnt = REFERRAL_CNT
                    while (errc == LDAP_ERR_REFERRAL) and ref_cnt:
                        # parse the first referral address from the output
                        clean_stde = stde.replace("\t","").split("\n")
                        referral_addr = clean_stde[clean_stde.index("referrals:") + 1]
                        if not referral_addr:
                            break
                        args = temp_args + ["-H", referral_addr]
                        (stdo,stde,errc) = ipautil.run(args, raiseonerr=False, nolog=nologlist)
                        ref_cnt -= 1

                    if errc:
                        if not ref_cnt:
                            root_logger.critical("Failed to load %s: Too many referrals" % ldif)
                        else:
                            root_logger.critical("Failed to load %s: %s" % (ldif, ' '.join(args + nologlist)))
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
        conn = self.admin_conn if self.on_master else self.master_conn

        dn = DN(('krbprincipalname', principal), ('cn', self.realm), ('cn', 'kerberos'), self.suffix)
        try:
            entry = conn.get_entry(dn)
        except errors.NotFound:
            # There is no service in the wrong location, nothing to do.
            # This can happen when installing a replica
            return None
        newdn = DN(('krbprincipalname', principal), ('cn', 'services'), ('cn', 'accounts'), self.suffix)
        hostdn = DN(('fqdn', self.fqdn), ('cn', 'computers'), ('cn', 'accounts'), self.suffix)
        conn.delete_entry(entry)
        entry.dn = newdn
        classes = entry.get("objectclass")
        classes = classes + ["ipaobject", "ipaservice", "pkiuser"]
        entry["objectclass"] = list(set(classes))
        entry["ipauniqueid"] = ['autogenerate']
        entry["managedby"] = [hostdn]
        conn.add_entry(entry)
        return newdn

    def add_simple_service(self, principal):
        """
        Add a very basic IPA service.

        The principal needs to be fully-formed: service/host@REALM
        """
        conn = self.admin_conn if self.on_master else self.master_conn
        if not conn:
            self.ldap_connect()
        conn = self.admin_conn if self.on_master else self.master_conn

        dn = DN(('krbprincipalname', principal), ('cn', 'services'), ('cn', 'accounts'), self.suffix)
        hostdn = DN(('fqdn', self.fqdn), ('cn', 'computers'), ('cn', 'accounts'), self.suffix)
        entry = conn.make_entry(
            dn,
            objectclass=[
                "krbprincipal", "krbprincipalaux", "krbticketpolicyaux",
                "ipaobject", "ipaservice", "pkiuser"],
            krbprincipalname=[principal],
            ipauniqueid=['autogenerate'],
            managedby=[hostdn],
        )
        conn.add_entry(entry)
        return dn

    def add_cert_to_service(self):
        """
        Add a certificate to a service

        This server cert should be in DER format.
        """

        # add_cert_to_service() is relatively rare operation
        # we actually call it twice during ipa-server-install, for different
        # instances: ds and cs. Unfortunately, it may happen that admin
        # connection was created well before add_cert_to_service() is called
        # If there are other operations in between, it will become stale and
        # since we are using SimpleLDAPObject, not ReconnectLDAPObject, the
        # action will fail. Thus, explicitly disconnect and connect again.
        # Using ReconnectLDAPObject instead of SimpleLDAPObject was considered
        # but consequences for other parts of the framework are largely
        # unknown.
        conn = self.admin_conn if self.on_master else self.master_conn
        if conn:
            self.ldap_disconnect()
        self.ldap_connect()
        conn = self.admin_conn if self.on_master else self.master_conn

        dn = DN(('krbprincipalname', self.principal), ('cn', 'services'),
                ('cn', 'accounts'), self.suffix)
        entry = conn.get_entry(dn)
        entry.setdefault('userCertificate', []).append(self.dercert)
        try:
            conn.update_entry(entry)
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

    def start_creation(self, start_message=None, end_message=None,
        show_service_name=True, runtime=-1):
        """
        Starts creation of the service.

        Use start_message and end_message for explicit messages
        at the beggining / end of the process. Otherwise they are generated
        using the service description (or service name, if the description has
        not been provided).

        Use show_service_name to include service name in generated descriptions.
        """

        if start_message is None:
            # no other info than mandatory service_name provided, use that
            if self.service_desc is None:
                start_message = "Configuring %s" % self.service_name

            # description should be more accurate than service name
            else:
                start_message = "Configuring %s" % self.service_desc
                if show_service_name:
                    start_message = "%s (%s)" % (start_message, self.service_name)

        if end_message is None:
            if self.service_desc is None:
                if show_service_name:
                    end_message = "Done configuring %s." % self.service_name
                else:
                    end_message = "Done."
            else:
                if show_service_name:
                    end_message = "Done configuring %s (%s)." % (
                        self.service_desc, self.service_name)
                else:
                    end_message = "Done configuring %s." % self.service_desc

        if runtime > 0:
            plural=''
            est = time.localtime(runtime)
            if est.tm_min > 0:
                if est.tm_min > 1:
                    plural = 's'
                if est.tm_sec > 0:
                    self.print_msg('%s: Estimated time %d minute%s %d seconds' % (start_message, est.tm_min, plural, est.tm_sec))
                else:
                    self.print_msg('%s: Estimated time %d minute%s' % (start_message, est.tm_min, plural))
            else:
                if est.tm_sec > 1:
                    plural = 's'
                self.print_msg('%s: Estimated time %d second%s' % (start_message, est.tm_sec, plural))
        else:
            self.print_msg(start_message)

        step = 0
        for (message, method) in self.steps:
            self.print_msg("  [%d/%d]: %s" % (step+1, len(self.steps), message))
            s = datetime.datetime.now()
            method()
            e = datetime.datetime.now()
            d = e - s
            root_logger.debug("  duration: %d seconds" % d.seconds)
            step += 1

        self.print_msg(end_message)

        self.steps = []

    def ldap_enable(self, name, fqdn, dm_password, ldap_suffix):
        assert isinstance(ldap_suffix, DN)
        self.disable()

        if not self.admin_conn or (not self.on_master and not self.master_conn):
            self.ldap_connect()

        server_group = "masters"
        if self.replica_type == "consumer":
            server_group = "consumers"
        elif self.replica_type == "hub":
            server_group = "hubs"

        entry_name = DN(('cn', name), ('cn', fqdn), ('cn', server_group), ('cn', 'ipa'), ('cn', 'etc'), ldap_suffix)
        order = SERVICE_LIST[name][1]
        entry = self.admin_conn.make_entry(
            entry_name,
            objectclass=["nsContainer", "ipaConfigObject"],
            cn=[name],
            ipaconfigstring=[
                "enabledService", "startOrder " + str(order)],
        )

        try:
            self.admin_conn.add_entry(entry)
            if not self.on_master:
                self.master_conn.add_entry(entry)
        except (errors.DuplicateEntry), e:
            root_logger.debug("failed to add %s Service startup entry" % name)
            raise e

class SimpleServiceInstance(Service):
    def create_instance(self, gensvc_name=None, fqdn=None, dm_password=None, ldap_suffix=None, realm=None, replica_type="master", master_fqdn=None):
        self.gensvc_name = gensvc_name
        self.fqdn = fqdn
        self.dm_password = dm_password
        self.master_fqdn = master_fqdn
        self.replica_type = replica_type
        self.suffix = ldap_suffix
        self.realm = realm
        if not realm:
            self.ldapi = False

        self._set_service_location(server_type=self.replica_type)

        self.step("starting %s " % self.service_name, self.__start)
        self.step("configuring %s to start on boot" % self.service_name, self.__enable)
        self.start_creation("Configuring %s" % self.service_name)

    suffix = ipautil.dn_attribute_property('_ldap_suffix')

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
