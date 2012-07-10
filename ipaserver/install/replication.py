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

import time
from ipapython.ipa_log_manager import *

import os
import sys
import ldap
from ipaserver import ipaldap
from ipapython import services as ipaservices
from ldap import modlist
from ipalib import api, util, errors
from ipapython import ipautil
from ipalib.dn import DN

DIRMAN_CN = "cn=directory manager"
CACERT = "/etc/ipa/ca.crt"
# the default container used by AD for user entries
WIN_USER_CONTAINER = "cn=Users"
# the default container used by IPA for user entries
IPA_USER_CONTAINER = "cn=users,cn=accounts"
PORT = 636
TIMEOUT = 120
REPL_MAN_DN = "cn=replication manager,cn=config"

IPA_REPLICA = 1
WINSYNC = 2

# types of replica
MASTER = 0
CONSUMER = 1
HUB = 2

def replica_conn_check(master_host, host_name, realm, check_ca,
                       admin_password=None):
    """
    Check the ports used by the replica both locally and remotely to be sure
    that replication will work.

    Does not return a value, will sys.exit() on failure.
    """
    print "Run connection check to master"
    args = ["/usr/sbin/ipa-replica-conncheck", "--master", master_host,
            "--auto-master-check", "--realm", realm,
            "--principal", "admin",
            "--hostname", host_name]
    nolog=tuple()

    if admin_password:
        args.extend(["--password", admin_password])
        nolog=(admin_password,)

    if check_ca:
        args.append('--check-ca')
    (stdin, stderr, returncode) = ipautil.run(args,raiseonerr=False,capture_output=False,
                                              nolog=nolog)

    if returncode != 0:
        sys.exit("Connection check failed!" +
                 "\nPlease fix your network settings according to error messages above." +
                 "\nIf the check results are not valid it can be skipped with --skip-conncheck parameter.")
    else:
        print "Connection check OK"

def enable_replication_version_checking(hostname, realm, dirman_passwd):
    """
    Check the replication version checking plugin. If it is not
    enabled then enable it and restart 389-ds. If it is enabled
    the do nothing.
    """
    conn = ipaldap.IPAdmin(hostname, realm=realm, ldapi=True)
    if dirman_passwd:
        conn.do_simple_bind(bindpw=dirman_passwd)
    else:
        conn.do_sasl_gssapi_bind()
    entry = conn.search_s('cn=IPA Version Replication,cn=plugins,cn=config', ldap.SCOPE_BASE, 'objectclass=*')
    if entry[0].getValue('nsslapd-pluginenabled') == 'off':
        conn.modify_s(entry[0].dn, [(ldap.MOD_REPLACE, 'nsslapd-pluginenabled', 'on')])
        conn.unbind()
        serverid = "-".join(realm.split("."))
        ipaservices.knownservices.dirsrv.restart(instance_name=serverid)
    else:
        conn.unbind()

class ReplicationManager(object):
    """Manage replication agreements between DS servers, and sync
    agreements with Windows servers"""
    def __init__(self, realm, hostname, dirman_passwd, port=PORT, starttls=False, conn=None):
        self.hostname = hostname
        self.port = port
        self.dirman_passwd = dirman_passwd
        self.realm = realm
        self.starttls = starttls
        tmp = ipautil.realm_to_suffix(realm)
        self.suffix = str(DN(tmp)).lower()
        self.need_memberof_fixup = False

        # The caller is allowed to pass in an existing IPAdmin connection.
        # Open a new one if not provided
        if conn is None:
            # If we are passed a password we'll use it as the DM password
            # otherwise we'll do a GSSAPI bind.
            if starttls:
                self.conn = ipaldap.IPAdmin(hostname, port=port)
                ldap.set_option(ldap.OPT_X_TLS_CACERTFILE, CACERT)
                self.conn.start_tls_s()
            else:
                self.conn = ipaldap.IPAdmin(hostname, port=port, cacert=CACERT)
            if dirman_passwd:
                self.conn.do_simple_bind(bindpw=dirman_passwd)
            else:
                self.conn.do_sasl_gssapi_bind()
        else:
            self.conn = conn

        self.repl_man_passwd = dirman_passwd

        # these are likely constant, but you could change them
        # at runtime if you really want
        self.repl_man_dn = REPL_MAN_DN
        self.repl_man_cn = "replication manager"

    def _get_replica_id(self, conn, master_conn):
        """
        Returns the replica ID which is unique for each backend.

        conn is the connection we are trying to get the replica ID for.
        master_conn is the master we are going to replicate with.
        """
        # First see if there is already one set
        dn = self.replica_dn()
        try:
            replica = conn.search_s(dn, ldap.SCOPE_BASE, "objectclass=*")[0]
            if replica.getValue('nsDS5ReplicaId'):
                return int(replica.getValue('nsDS5ReplicaId'))
        except ldap.NO_SUCH_OBJECT:
            pass

        # Ok, either the entry doesn't exist or the attribute isn't set
        # so get it from the other master
        retval = -1
        dn = str(DN(('cn','replication'),('cn','etc'), self.suffix))
        try:
            replica = master_conn.search_s(dn, ldap.SCOPE_BASE, "objectclass=*")[0]
            if not replica.getValue('nsDS5ReplicaId'):
                root_logger.debug("Unable to retrieve nsDS5ReplicaId from remote server")
                raise RuntimeError("Unable to retrieve nsDS5ReplicaId from remote server")
        except ldap.NO_SUCH_OBJECT:
            root_logger.debug("Unable to retrieve nsDS5ReplicaId from remote server")
            raise

        # Now update the value on the master
        retval = int(replica.getValue('nsDS5ReplicaId'))
        mod = [(ldap.MOD_REPLACE, 'nsDS5ReplicaId', str(retval + 1))]

        try:
            master_conn.modify_s(dn, mod)
        except Exception, e:
            root_logger.debug("Problem updating nsDS5ReplicaID %s" % e)
            raise

        return retval

    def find_replication_agreements(self):
        """
        The replication agreements are stored in
        cn="$SUFFIX",cn=mapping tree,cn=config

        FIXME: Rather than failing with a read error if a user tries
        to read this it simply returns zero entries. We need to use
        GER to determine if we are allowed to read this to return a proper
        response. For now just return "No entries" even if the user may
        not be allowed to see them.
        """
        filt = "(|(objectclass=nsDSWindowsReplicationAgreement)(objectclass=nsds5ReplicationAgreement))"
        try:
            ents = self.conn.search_s("cn=mapping tree,cn=config", ldap.SCOPE_SUBTREE, filt)
        except ldap.NO_SUCH_OBJECT:
            ents = []
        return ents

    def find_ipa_replication_agreements(self):
        """
        The replication agreements are stored in
        cn="$SUFFIX",cn=mapping tree,cn=config

        Return the list of hosts we have replication agreements.
        """

        res = []

        filt = "(objectclass=nsds5ReplicationAgreement)"
        try:
            ents = self.conn.search_s("cn=mapping tree,cn=config",
                                      ldap.SCOPE_SUBTREE, filt)
        except ldap.NO_SUCH_OBJECT:
            return res

        for ent in ents:
            res.append(ent.nsds5replicahost)

        return res

    def get_replication_agreement(self, hostname):
        """
        The replication agreements are stored in
        cn="$SUFFIX",cn=mapping tree,cn=config

        Get the replication agreement for a specific host.

        Returns None if not found.
        """

        filt = "(&(|(objectclass=nsds5ReplicationAgreement)(objectclass=nsDSWindowsReplicationAgreement))(nsDS5ReplicaHost=%s))" % hostname
        try:
            entry = self.conn.search_s("cn=mapping tree,cn=config",
                                       ldap.SCOPE_SUBTREE, filt)
        except ldap.NO_SUCH_OBJECT:
            return None

        if len(entry) == 0:
            return None
        else:
            return entry[0] # There can be only one

    def add_replication_manager(self, conn, dn, pw):
        """
        Create a pseudo user to use for replication.
        """

        edn = ldap.dn.str2dn(dn)
        rdn_attr = edn[0][0][0]
        rdn_val = edn[0][0][1]

        ent = ipaldap.Entry(dn)
        ent.setValues("objectclass", "top", "person")
        ent.setValues(rdn_attr, rdn_val)
        ent.setValues("userpassword", pw)
        ent.setValues("sn", "replication manager pseudo user")

        try:
            conn.addEntry(ent)
        except errors.DuplicateEntry:
            conn.modify_s(dn, [(ldap.MOD_REPLACE, "userpassword", pw)])
            pass

    def delete_replication_manager(self, conn, dn=REPL_MAN_DN):
        try:
            conn.delete_s(dn)
        except ldap.NO_SUCH_OBJECT:
            pass

    def get_replica_type(self, master=True):
        if master:
            return "3"
        else:
            return "2"

    def replica_dn(self):
        return str(DN(('cn','replica'),('cn',self.suffix),('cn','mapping tree'),('cn','config')))

    def replica_config(self, conn, replica_id, replica_binddn):
        dn = self.replica_dn()

        try:
            entry = conn.getEntry(dn, ldap.SCOPE_BASE)
            managers = entry.getValues('nsDS5ReplicaBindDN')
            for m in managers:
                if DN(replica_binddn) == DN(m):
                    return
            # Add the new replication manager
            mod = [(ldap.MOD_ADD, 'nsDS5ReplicaBindDN', replica_binddn)]
            conn.modify_s(dn, mod)

            # replication is already configured
            return
        except errors.NotFound:
            pass

        replica_type = self.get_replica_type()

        entry = ipaldap.Entry(dn)
        entry.setValues('objectclass', "top", "nsds5replica", "extensibleobject")
        entry.setValues('cn', "replica")
        entry.setValues('nsds5replicaroot', self.suffix)
        entry.setValues('nsds5replicaid', str(replica_id))
        entry.setValues('nsds5replicatype', replica_type)
        entry.setValues('nsds5flags', "1")
        entry.setValues('nsds5replicabinddn', [replica_binddn])
        entry.setValues('nsds5replicalegacyconsumer', "off")

        conn.addEntry(entry)

    def setup_changelog(self, conn):
        dn = "cn=changelog5, cn=config"
        dirpath = conn.dbdir + "/cldb"
        entry = ipaldap.Entry(dn)
        entry.setValues('objectclass', "top", "extensibleobject")
        entry.setValues('cn', "changelog5")
        entry.setValues('nsslapd-changelogdir', dirpath)
        try:
            conn.addEntry(entry)
        except errors.DuplicateEntry:
            return

    def setup_chaining_backend(self, conn):
        chaindn = "cn=chaining database, cn=plugins, cn=config"
        benamebase = "chaindb"
        urls = [self.to_ldap_url(conn)]
        cn = ""
        benum = 1
        done = False
        while not done:
            try:
                cn = benamebase + str(benum) # e.g. localdb1
                dn = "cn=" + cn + ", " + chaindn
                entry = ipaldap.Entry(dn)
                entry.setValues('objectclass', 'top', 'extensibleObject', 'nsBackendInstance')
                entry.setValues('cn', cn)
                entry.setValues('nsslapd-suffix', self.suffix)
                entry.setValues('nsfarmserverurl', urls)
                entry.setValues('nsmultiplexorbinddn', self.repl_man_dn)
                entry.setValues('nsmultiplexorcredentials', self.repl_man_passwd)

                self.conn.addEntry(entry)
                done = True
            except errors.DuplicateEntry:
                benum += 1
            except errors.ExecutionError, e:
                print "Could not add backend entry " + dn, e
                raise

        return cn

    def to_ldap_url(self, conn):
        return "ldap://%s/" % ipautil.format_netloc(conn.host, conn.port)

    def setup_chaining_farm(self, conn):
        try:
            conn.modify_s(self.suffix, [(ldap.MOD_ADD, 'aci',
                                    [ "(targetattr = \"*\")(version 3.0; acl \"Proxied authorization for database links\"; allow (proxy) userdn = \"ldap:///%s\";)" % self.repl_man_dn ])])
        except ldap.TYPE_OR_VALUE_EXISTS:
            root_logger.debug("proxy aci already exists in suffix %s on %s" % (self.suffix, conn.host))

    def get_mapping_tree_entry(self):
        try:
            entry = self.conn.getEntry("cn=mapping tree,cn=config", ldap.SCOPE_ONELEVEL,
                                       "(cn=\"%s\")" % (self.suffix))
        except errors.NotFound, e:
            root_logger.debug("failed to find mappting tree entry for %s" % self.suffix)
            raise e

        return entry


    def enable_chain_on_update(self, bename):
        mtent = self.get_mapping_tree_entry()
        dn = mtent.dn

        plgent = self.conn.getEntry("cn=Multimaster Replication Plugin,cn=plugins,cn=config",
                                    ldap.SCOPE_BASE, "(objectclass=*)", ['nsslapd-pluginPath'])
        path = plgent.getValue('nsslapd-pluginPath')

        mod = [(ldap.MOD_REPLACE, 'nsslapd-state', 'backend'),
               (ldap.MOD_ADD, 'nsslapd-backend', bename),
               (ldap.MOD_ADD, 'nsslapd-distribution-plugin', path),
               (ldap.MOD_ADD, 'nsslapd-distribution-funct', 'repl_chain_on_update')]

        try:
            self.conn.modify_s(dn, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            root_logger.debug("chainOnUpdate already enabled for %s" % self.suffix)

    def setup_chain_on_update(self, other_conn):
        chainbe = self.setup_chaining_backend(other_conn)
        self.enable_chain_on_update(chainbe)

    def add_passsync_user(self, conn, password):
        pass_dn = "uid=passsync,cn=sysaccounts,cn=etc,%s" % self.suffix
        print "The user for the Windows PassSync service is %s" % pass_dn
        try:
            conn.getEntry(pass_dn, ldap.SCOPE_BASE)
            print "Windows PassSync entry exists, not resetting password"
            return
        except errors.NotFound:
            pass

        # The user doesn't exist, add it
        entry = ipaldap.Entry(pass_dn)
        entry.setValues("objectclass", ["account", "simplesecurityobject"])
        entry.setValues("uid", "passsync")
        entry.setValues("userPassword", password)
        conn.addEntry(entry)

        # Add it to the list of users allowed to bypass password policy
        extop_dn = "cn=ipa_pwd_extop,cn=plugins,cn=config"
        entry = conn.getEntry(extop_dn, ldap.SCOPE_BASE)
        pass_mgrs = entry.getValues('passSyncManagersDNs')
        if not pass_mgrs:
            pass_mgrs = []
        if not isinstance(pass_mgrs, list):
            pass_mgrs = [pass_mgrs]
        pass_mgrs.append(pass_dn)
        mod = [(ldap.MOD_REPLACE, 'passSyncManagersDNs', pass_mgrs)]
        conn.modify_s(extop_dn, mod)

        # And finally grant it permission to write passwords
        mod = [(ldap.MOD_ADD, 'aci',
            ['(targetattr = "userPassword || krbPrincipalKey || sambaLMPassword || sambaNTPassword || passwordHistory")(version 3.0; acl "Windows PassSync service can write passwords"; allow (write) userdn="ldap:///%s";)' % pass_dn])]
        try:
            conn.modify_s(self.suffix, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            root_logger.debug("passsync aci already exists in suffix %s on %s" % (self.suffix, conn.host))

    def setup_winsync_agmt(self, entry, win_subtree=None):
        if win_subtree is None:
            win_subtree = WIN_USER_CONTAINER + "," + self.ad_suffix
        ds_subtree = IPA_USER_CONTAINER + "," + self.suffix
        windomain = '.'.join(ldap.explode_dn(self.suffix, 1))

        entry.setValues("objectclass", "nsDSWindowsReplicationAgreement")
        entry.setValues("nsds7WindowsReplicaSubtree", win_subtree)
        entry.setValues("nsds7DirectoryReplicaSubtree", ds_subtree)
        # for now, just sync users and ignore groups
        entry.setValues("nsds7NewWinUserSyncEnabled", 'true')
        entry.setValues("nsds7NewWinGroupSyncEnabled", 'false')
        entry.setValues("nsds7WindowsDomain", windomain)

    def agreement_dn(self, hostname, master=None):
        """
        IPA agreement use the same dn on both sides, dogtag does not.
        master is not used for IPA agreements but for dogtag it will
        tell which side we want.
        """
        cn = "meTo%s" % (hostname)
        dn = "cn=%s, %s" % (cn, self.replica_dn())

        return (cn, dn)

    def setup_agreement(self, a_conn, b_hostname, port=389,
                        repl_man_dn=None, repl_man_passwd=None,
                        iswinsync=False, win_subtree=None, isgssapi=False,
                        master=None):
        """
        master is used to determine which side of the agreement we are
        creating. This is only needed for dogtag replication agreements
        which use a different name on each side. If master is None then
        isn't a dogtag replication agreement.
        """

        cn, dn = self.agreement_dn(b_hostname, master=master)
        try:
            a_conn.getEntry(dn, ldap.SCOPE_BASE)
            return
        except errors.NotFound:
            pass

        # List of attributes that need to be excluded from replication initialization.
        totalexcludes = ('entryusn',
                         'krblastsuccessfulauth',
                         'krblastfailedauth',
                         'krbloginfailedcount')

        # List of attributes that need to be excluded from normal replication.
        excludes = ('memberof', ) + totalexcludes

        entry = ipaldap.Entry(dn)
        entry.setValues('objectclass', "nsds5replicationagreement")
        entry.setValues('cn', cn)
        entry.setValues('nsds5replicahost', b_hostname)
        entry.setValues('nsds5replicaport', str(port))
        entry.setValues('nsds5replicatimeout', str(TIMEOUT))
        entry.setValues('nsds5replicaroot', self.suffix)
        if master is None:
            entry.setValues('nsDS5ReplicatedAttributeList',
                            '(objectclass=*) $ EXCLUDE %s' % " ".join(excludes))
        entry.setValues('description', "me to %s" % b_hostname)
        if isgssapi:
            entry.setValues('nsds5replicatransportinfo', 'LDAP')
            entry.setValues('nsds5replicabindmethod', 'SASL/GSSAPI')
        else:
            entry.setValues('nsds5replicabinddn', repl_man_dn)
            entry.setValues('nsds5replicacredentials', repl_man_passwd)
            entry.setValues('nsds5replicatransportinfo', 'TLS')
            entry.setValues('nsds5replicabindmethod', 'simple')

        if iswinsync:
            self.setup_winsync_agmt(entry, win_subtree)

        a_conn.addEntry(entry)

        try:
            mod = [(ldap.MOD_ADD, 'nsDS5ReplicatedAttributeListTotal',
                   '(objectclass=*) $ EXCLUDE %s' % " ".join(totalexcludes))]
            a_conn.modify_s(dn, mod)
        except ldap.LDAPError, e:
            # Apparently there are problems set the total list
            # Probably the master is an old 389-ds server, tell the caller
            # that we will have to set the memberof fixup task
            self.need_memberof_fixup = True

        entry = a_conn.waitForEntry(entry)

    def needs_memberof_fixup(self):
        return self.need_memberof_fixup

    def get_replica_principal_dns(self, a, b, retries):
        """
        Get the DNs of the ldap principals we are going to convert
        to using GSSAPI replication.

        Arguments a and b are LDAP connections. retries is the number
        of attempts that should be made to find the entries. It could
        be that replication is slow.

        If successful this returns a tuple (dn_a, dn_b).

        If either of the DNs doesn't exist after the retries are
        exhausted an exception is raised.
        """
        filter_a = '(krbprincipalname=ldap/%s@%s)' % (a.host, self.realm)
        filter_b = '(krbprincipalname=ldap/%s@%s)' % (b.host, self.realm)

        a_entry = None
        b_entry = None

        while (retries > 0 ):
            root_logger.info('Getting ldap service principals for conversion: %s and %s' % (filter_a, filter_b))
            a_entry = b.search_s(self.suffix, ldap.SCOPE_SUBTREE,
                                 filterstr=filter_a)
            b_entry = a.search_s(self.suffix, ldap.SCOPE_SUBTREE,
                                 filterstr=filter_b)

            if a_entry and b_entry:
                root_logger.debug('Found both principals.')
                break

            # One or both is missing, force sync again
            if not a_entry:
                root_logger.debug('Unable to find entry for %s on %s'
                    % (filter_a, str(b)))
                self.force_sync(a, b.host)
                cn, dn = self.agreement_dn(b.host)
                self.wait_for_repl_update(a, dn, 30)

            if not b_entry:
                root_logger.debug('Unable to find entry for %s on %s'
                    % (filter_b, str(a)))
                self.force_sync(b, a.host)
                cn, dn = self.agreement_dn(a.host)
                self.wait_for_repl_update(b, dn, 30)

            retries -= 1

        if not a_entry or not b_entry:
            raise RuntimeError('One of the ldap service principals is missing. Replication agreement cannot be converted')

        return (a_entry[0].dn, b_entry[0].dn)

    def setup_krb_princs_as_replica_binddns(self, a, b):
        """
        Search the appropriate principal names so we can get
        the correct DNs to store in the replication agreements.
        Then modify the replica object to allow these DNs to act
        as replication agents.
        """

        rep_dn = self.replica_dn()
        (a_dn, b_dn) = self.get_replica_principal_dns(a, b, retries=10)

        # Add kerberos principal DNs as valid bindDNs for replication
        try:
            mod = [(ldap.MOD_ADD, "nsds5replicabinddn", b_dn)]
            a.modify_s(rep_dn, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            pass
        try:
            mod = [(ldap.MOD_ADD, "nsds5replicabinddn", a_dn)]
            b.modify_s(rep_dn, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            pass

    def gssapi_update_agreements(self, a, b):

        self.setup_krb_princs_as_replica_binddns(a, b)

        #change replication agreements to connect to other host using GSSAPI
        mod = [(ldap.MOD_REPLACE, "nsds5replicatransportinfo", "LDAP"),
               (ldap.MOD_REPLACE, "nsds5replicabindmethod", "SASL/GSSAPI"),
               (ldap.MOD_DELETE, "nsds5replicabinddn", None),
               (ldap.MOD_DELETE, "nsds5replicacredentials", None)]

        cn, a_ag_dn = self.agreement_dn(b.host)
        a.modify_s(a_ag_dn, mod)

        cn, b_ag_dn = self.agreement_dn(a.host)
        b.modify_s(b_ag_dn, mod)

        # Finally remove the temporary replication manager user
        try:
            a.delete_s(self.repl_man_dn)
        except ldap.NO_SUCH_OBJECT:
            pass
        try:
            b.delete_s(self.repl_man_dn)
        except ldap.NO_SUCH_OBJECT:
            pass

    def delete_agreement(self, hostname):
        cn, dn = self.agreement_dn(hostname)
        return self.conn.deleteEntry(dn)

    def delete_referral(self, hostname):
        esc1_suffix = self.suffix.replace('=', '\\3D').replace(',', '\\2C')
        esc2_suffix = self.suffix.replace('=', '%3D').replace(',', '%2C')
        dn = 'cn=%s,cn=mapping tree,cn=config' % esc1_suffix
        # TODO: should we detect proto/port somehow ?
        mod = [(ldap.MOD_DELETE, 'nsslapd-referral',
                'ldap://%s/%s' % (ipautil.format_netloc(hostname, 389), esc2_suffix))]

        try:
            self.conn.modify_s(dn, mod)
        except Exception, e:
            root_logger.debug("Failed to remove referral value: %s" % str(e))

    def check_repl_init(self, conn, agmtdn):
        done = False
        hasError = 0
        attrlist = ['cn', 'nsds5BeginReplicaRefresh',
                    'nsds5replicaUpdateInProgress',
                    'nsds5ReplicaLastInitStatus',
                    'nsds5ReplicaLastInitStart',
                    'nsds5ReplicaLastInitEnd']
        entry = conn.getEntry(agmtdn, ldap.SCOPE_BASE, "(objectclass=*)", attrlist)
        if not entry:
            print "Error reading status from agreement", agmtdn
            hasError = 1
        else:
            refresh = entry.nsds5BeginReplicaRefresh
            inprogress = entry.nsds5replicaUpdateInProgress
            status = entry.nsds5ReplicaLastInitStatus
            if not refresh: # done - check status
                if not status:
                    print "No status yet"
                elif status.find("replica busy") > -1:
                    print "[%s] reports: Replica Busy! Status: [%s]" % (conn.host, status)
                    done = True
                    hasError = 2
                elif status.find("Total update succeeded") > -1:
                    print "Update succeeded"
                    done = True
                elif inprogress.lower() == 'true':
                    print "Update in progress yet not in progress"
                else:
                    print "[%s] reports: Update failed! Status: [%s]" % (conn.host, status)
                    hasError = 1
                    done = True
            else:
                print "Update in progress"

        return done, hasError

    def check_repl_update(self, conn, agmtdn):
        done = False
        hasError = 0
        attrlist = ['cn', 'nsds5replicaUpdateInProgress',
                    'nsds5ReplicaLastUpdateStatus', 'nsds5ReplicaLastUpdateStart',
                    'nsds5ReplicaLastUpdateEnd']
        entry = conn.getEntry(agmtdn, ldap.SCOPE_BASE, "(objectclass=*)", attrlist)
        if not entry:
            print "Error reading status from agreement", agmtdn
            hasError = 1
        else:
            inprogress = entry.nsds5replicaUpdateInProgress
            status = entry.nsds5ReplicaLastUpdateStatus
            start = entry.nsds5ReplicaLastUpdateStart
            end = entry.nsds5ReplicaLastUpdateEnd
            # incremental update is done if inprogress is false and end >= start
            done = inprogress and inprogress.lower() == 'false' and start and end and (start <= end)
            root_logger.info("Replication Update in progress: %s: status: %s: start: %s: end: %s" %
                         (inprogress, status, start, end))
            if not done and status: # check for errors
                # status will usually be a number followed by a string
                # number != 0 means error
                rc, msg = status.split(' ', 1)
                if rc != '0':
                    hasError = 1
                    done = True

        return done, hasError

    def wait_for_repl_init(self, conn, agmtdn):
        done = False
        haserror = 0
        while not done and not haserror:
            time.sleep(1)  # give it a few seconds to get going
            done, haserror = self.check_repl_init(conn, agmtdn)
        return haserror

    def wait_for_repl_update(self, conn, agmtdn, maxtries=600):
        done = False
        haserror = 0
        while not done and not haserror and maxtries > 0:
            time.sleep(1)  # give it a few seconds to get going
            done, haserror = self.check_repl_update(conn, agmtdn)
            maxtries -= 1
        if maxtries == 0: # too many tries
            print "Error: timeout: could not determine agreement status: please check your directory server logs for possible errors"
            haserror = 1
        return haserror

    def start_replication(self, conn, hostname=None, master=None):
        print "Starting replication, please wait until this has completed."
        if hostname == None:
            hostname = self.conn.host
        cn, dn = self.agreement_dn(hostname, master)

        mod = [(ldap.MOD_ADD, 'nsds5BeginReplicaRefresh', 'start')]
        conn.modify_s(dn, mod)

        return self.wait_for_repl_init(conn, dn)

    def basic_replication_setup(self, conn, replica_id, repldn, replpw):
        if replpw is not None:
            self.add_replication_manager(conn, repldn, replpw)
        self.replica_config(conn, replica_id, repldn)
        self.setup_changelog(conn)

    def setup_replication(self, r_hostname, r_port=389, r_sslport=636,
                          r_binddn=None, r_bindpw=None, starttls=False,
                          is_cs_replica=False):
        # note - there appears to be a bug in python-ldap - it does not
        # allow connections using two different CA certs
        if starttls:
            r_conn = ipaldap.IPAdmin(r_hostname, port=r_port)
            ldap.set_option(ldap.OPT_X_TLS_CACERTFILE, CACERT)
            r_conn.start_tls_s()
        else:
            r_conn = ipaldap.IPAdmin(r_hostname, port=r_sslport, cacert=CACERT)

        if r_bindpw:
            r_conn.do_simple_bind(binddn=r_binddn, bindpw=r_bindpw)
        else:
            r_conn.do_sasl_gssapi_bind()

        #Setup the first half
        l_id = self._get_replica_id(self.conn, r_conn)
        self.basic_replication_setup(self.conn, l_id,
                                     self.repl_man_dn, self.repl_man_passwd)

        # Now setup the other half
        r_id = self._get_replica_id(r_conn, r_conn)
        self.basic_replication_setup(r_conn, r_id,
                                     self.repl_man_dn, self.repl_man_passwd)

        if is_cs_replica:
            self.setup_agreement(r_conn, self.conn.host, port=r_port,
                                 repl_man_dn=self.repl_man_dn,
                                 repl_man_passwd=self.repl_man_passwd,
                                 master=True)
            self.setup_agreement(self.conn, r_hostname, port=r_port,
                                 repl_man_dn=self.repl_man_dn,
                                 repl_man_passwd=self.repl_man_passwd,
                                 master=False)
        else:
            self.setup_agreement(r_conn, self.conn.host, port=r_port,
                                 repl_man_dn=self.repl_man_dn,
                                 repl_man_passwd=self.repl_man_passwd)
            self.setup_agreement(self.conn, r_hostname, port=r_port,
                                 repl_man_dn=self.repl_man_dn,
                                 repl_man_passwd=self.repl_man_passwd)

        #Finally start replication
        ret = self.start_replication(r_conn, master=True)
        if ret != 0:
            raise RuntimeError("Failed to start replication")

    def setup_winsync_replication(self,
                                  ad_dc_name, ad_binddn, ad_pwd,
                                  passsync_pw, ad_subtree,
                                  cacert=CACERT):
        self.ad_suffix = ""
        try:
            # Validate AD connection
            ad_conn = ldap.initialize('ldap://%s' % ipautil.format_netloc(ad_dc_name))
            # the next one is to workaround bugs arounf opendalp libs+NSS db
            # we need to first specify the OPT_X_TLS_CACERTFILE and _after_
            # that initialize the context to prevent TLS connection errors:
            # https://bugzilla.redhat.com/show_bug.cgi?id=800787
            ad_conn.set_option(ldap.OPT_X_TLS_CACERTFILE, cacert)
            ad_conn.set_option(ldap.OPT_X_TLS_NEWCTX, 0)
            ad_conn.start_tls_s()
            ad_conn.simple_bind_s(ad_binddn, ad_pwd)
            res = ad_conn.search_s("", ldap.SCOPE_BASE, '(objectClass=*)',
                                   ['defaultNamingContext'])
            for dn,entry in res:
                if dn == "":
                    self.ad_suffix = entry['defaultNamingContext'][0]
                    root_logger.info("AD Suffix is: %s" % self.ad_suffix)
            if self.ad_suffix == "":
                raise RuntimeError("Failed to lookup AD's Ldap suffix")
            ad_conn.unbind_s()
            del ad_conn
        except Exception, e:
            root_logger.info("Failed to connect to AD server %s" % ad_dc_name)
            root_logger.info("The error was: %s" % e)
            raise RuntimeError("Failed to setup winsync replication")

        # Setup the only half.
        # there is no other side to get a replica ID from
        # So we generate one locally
        replica_id = self._get_replica_id(self.conn, self.conn)
        self.basic_replication_setup(self.conn, replica_id,
                                     self.repl_man_dn, self.repl_man_passwd)

        #now add a passync user allowed to access the AD server
        self.add_passsync_user(self.conn, passsync_pw)
        self.setup_agreement(self.conn, ad_dc_name,
                             repl_man_dn=ad_binddn, repl_man_passwd=ad_pwd,
                             iswinsync=True, win_subtree=ad_subtree)
        root_logger.info("Added new sync agreement, waiting for it to become ready . . .")
        cn, dn = self.agreement_dn(ad_dc_name)
        self.wait_for_repl_update(self.conn, dn, 300)
        root_logger.info("Agreement is ready, starting replication . . .")

        # Add winsync replica to the public DIT
        dn = str(DN(('cn',ad_dc_name),('cn','replicas'),('cn','ipa'),('cn','etc'), self.suffix))
        entry = ipaldap.Entry(dn)
        entry.setValues("objectclass", ["nsContainer", "ipaConfigObject"])
        entry.setValues("cn", ad_dc_name)
        entry.setValues("ipaConfigString", "winsync:%s" % self.hostname)

        try:
            self.conn.addEntry(entry)
        except Exception, e:
            root_logger.info("Failed to create public entry for winsync replica")

        #Finally start replication
        ret = self.start_replication(self.conn, ad_dc_name)
        if ret != 0:
            raise RuntimeError("Failed to start replication")

    def convert_to_gssapi_replication(self, r_hostname, r_binddn, r_bindpw):
        r_conn = ipaldap.IPAdmin(r_hostname, port=PORT, cacert=CACERT)
        if r_bindpw:
            r_conn.do_simple_bind(binddn=r_binddn, bindpw=r_bindpw)
        else:
            r_conn.do_sasl_gssapi_bind()

        # First off make sure servers are in sync so that both KDCs
        # have all principals and their passwords and can release
        # the right tickets. We do this by force pushing all our changes
        self.force_sync(self.conn, r_hostname)
        cn, dn = self.agreement_dn(r_hostname)
        self.wait_for_repl_update(self.conn, dn, 300)

        # now in the opposite direction
        self.force_sync(r_conn, self.hostname)
        cn, dn = self.agreement_dn(self.hostname)
        self.wait_for_repl_update(r_conn, dn, 300)

        # now that directories are in sync,
        # change the agreements to use GSSAPI
        self.gssapi_update_agreements(self.conn, r_conn)

    def setup_gssapi_replication(self, r_hostname, r_binddn=None, r_bindpw=None):
        """
        Directly sets up GSSAPI replication.
        Only usable to connect 2 existing replicas (needs existing kerberos
        principals)
        """
        # note - there appears to be a bug in python-ldap - it does not
        # allow connections using two different CA certs
        r_conn = ipaldap.IPAdmin(r_hostname, port=PORT, cacert=CACERT)
        if r_bindpw:
            r_conn.do_simple_bind(binddn=r_binddn, bindpw=r_bindpw)
        else:
            r_conn.do_sasl_gssapi_bind()

        # Allow krb principals to act as replicas
        self.setup_krb_princs_as_replica_binddns(self.conn, r_conn)

        # Create mutual replication agreementsausiung SASL/GSSAPI
        self.setup_agreement(self.conn, r_hostname, isgssapi=True)
        self.setup_agreement(r_conn, self.conn.host, isgssapi=True)

    def initialize_replication(self, dn, conn):
        mod = [(ldap.MOD_ADD, 'nsds5BeginReplicaRefresh', 'start')]
        try:
            conn.modify_s(dn, mod)
        except ldap.ALREADY_EXISTS:
            return

    def force_sync(self, conn, hostname):

        newschedule = '2358-2359 0'

        filter = '(&(nsDS5ReplicaHost=%s)' \
                   '(|(objectclass=nsDSWindowsReplicationAgreement)' \
                     '(objectclass=nsds5ReplicationAgreement)))' % hostname
        entry = conn.search_s("cn=config", ldap.SCOPE_SUBTREE, filter)
        if len(entry) == 0:
            root_logger.error("Unable to find replication agreement for %s" %
                          (hostname))
            raise RuntimeError("Unable to proceed")
        if len(entry) > 1:
            root_logger.error("Found multiple agreements for %s" % hostname)
            root_logger.error("Using the first one only (%s)" % entry[0].dn)

        dn = entry[0].dn
        schedule = entry[0].nsds5replicaupdateschedule

        # On the remote chance of a match. We force a synch to happen right
        # now by setting the schedule to something and quickly removing it.
        if schedule is not None:
            if newschedule == schedule:
                newschedule = '2358-2359 1'
        root_logger.info("Setting agreement %s schedule to %s to force synch" %
                     (dn, newschedule))
        mod = [(ldap.MOD_REPLACE, 'nsDS5ReplicaUpdateSchedule', [ newschedule ])]
        conn.modify_s(dn, mod)
        time.sleep(1)
        root_logger.info("Deleting schedule %s from agreement %s" %
                     (newschedule, dn))
        mod = [(ldap.MOD_DELETE, 'nsDS5ReplicaUpdateSchedule', None)]
        conn.modify_s(dn, mod)

    def get_agreement_type(self, hostname):
        cn, dn = self.agreement_dn(hostname)

        entry = self.conn.getEntry(dn, ldap.SCOPE_BASE)

        objectclass = entry.getValues("objectclass")

        for o in objectclass:
            if o.lower() == "nsdswindowsreplicationagreement":
                return WINSYNC

        return IPA_REPLICA

    def replica_cleanup(self, replica, realm, force=False):
        """
        This function removes information about the replica in parts
        of the shared tree that expose it, so clients stop trying to
        use this replica.
        """

        err = None

        if replica == self.hostname:
            raise RuntimeError("Can't cleanup self")

        # delete master kerberos key and all its svc principals
        try:
            filter='(krbprincipalname=*/%s@%s)' % (replica, realm)
            entries = self.conn.search_s(self.suffix, ldap.SCOPE_SUBTREE,
                                         filterstr=filter)
            if len(entries) != 0:
                dnset = self.conn.get_dns_sorted_by_length(entries,
                                                           reverse=True)
                for dns in dnset:
                    for dn in dns:
                        self.conn.deleteEntry(dn)
        except ldap.NO_SUCH_OBJECT:
            pass
        except errors.NotFound:
            pass
        except Exception, e:
            if not force:
                raise e
            else:
                err = e

        # remove replica memberPrincipal from s4u2proxy configuration
        dn1 = DN(u'cn=ipa-http-delegation', api.env.container_s4u2proxy, self.suffix)
        member_principal1 = "HTTP/%(fqdn)s@%(realm)s" % dict(fqdn=replica, realm=realm)

        dn2 = DN(u'cn=ipa-ldap-delegation-targets', api.env.container_s4u2proxy, self.suffix)
        member_principal2 = "ldap/%(fqdn)s@%(realm)s" % dict(fqdn=replica, realm=realm)

        dn3 = DN(u'cn=ipa-cifs-delegation-targets', api.env.container_s4u2proxy, self.suffix)
        member_principal3 = "cifs/%(fqdn)s@%(realm)s" % dict(fqdn=replica, realm=realm)

        for (dn, member_principal) in ((str(dn1), member_principal1),
                                       (str(dn2), member_principal2),
                                       (str(dn3), member_principal3)):
            try:
                mod = [(ldap.MOD_DELETE, 'memberPrincipal', member_principal)]
                self.conn.modify_s(dn, mod)
            except (ldap.NO_SUCH_OBJECT, ldap.NO_SUCH_ATTRIBUTE):
                root_logger.debug("Replica (%s) memberPrincipal (%s) not found in %s" % \
                        (replica, member_principal, dn))
            except Exception, e:
                if not force:
                    raise e
                elif not err:
                    err = e

        # delete master entry with all active services
        try:
            dn = 'cn=%s,cn=masters,cn=ipa,cn=etc,%s' % (replica, self.suffix)
            entries = self.conn.search_s(dn, ldap.SCOPE_SUBTREE)
            if len(entries) != 0:
                dnset = self.conn.get_dns_sorted_by_length(entries,
                                                           reverse=True)
                for dns in dnset:
                    for dn in dns:
                        self.conn.deleteEntry(dn)
        except ldap.NO_SUCH_OBJECT:
            pass
        except errors.NotFound:
            pass
        except Exception, e:
            if not force:
                raise e
            elif not err:
                err = e

        try:
            basedn = 'cn=etc,%s' % self.suffix
            filter = '(dnaHostname=%s)' % replica
            entries = self.conn.search_s(basedn, ldap.SCOPE_SUBTREE,
                                         filterstr=filter)
            if len(entries) != 0:
                for e in entries:
                    self.conn.deleteEntry(e.dn)
        except ldap.NO_SUCH_OBJECT:
            pass
        except errors.NotFound:
            pass
        except Exception, e:
            if not force:
                raise e
            elif not err:
                err = e

        try:
            dn = 'cn=default,ou=profile,%s' % self.suffix
            ret = self.conn.search_s(dn, ldap.SCOPE_BASE,
                                     '(objectclass=*)')[0]
            srvlist = ret.data.get('defaultServerList')
            if len(srvlist) > 0:
                srvlist = srvlist[0].split()
            if replica in srvlist:
                srvlist.remove(replica)
                attr = ' '.join(srvlist)
                mod = [(ldap.MOD_REPLACE, 'defaultServerList', attr)]
                self.conn.modify_s(dn, mod)
        except ldap.NO_SUCH_OBJECT:
            pass
        except ldap.NO_SUCH_ATTRIBUTE:
            pass
        except ldap.TYPE_OR_VALUE_EXISTS:
            pass
        except Exception, e:
            if force and err:
                raise err   #pylint: disable=E0702
            else:
                raise e

        if err:
            raise err   #pylint: disable=E0702
