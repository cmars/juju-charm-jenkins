import grp
import hashlib
import os
import pwd
import subprocess

from charms.reactive import when, when_not, set_state

from charmhelpers.core.hookenv import (
    Hooks,
    status_set,
    config,
    remote_unit,
    related_units,
    relation_get,
    relation_set,
    relation_ids,
    unit_get,
    unit_public_ip,
    open_port,
    log,
    DEBUG,
    INFO,
)

from charmhelpers.core.host import (
    service_restart,
    service_start,
    service_stop,
)
from charmhelpers.core.templating import render
from charms.layer.jenkins import (
    JENKINS_HOME,
    JENKINS_USERS,
    TEMPLATES_DIR,
    add_node,
    del_node,
    get_jenkins_password,
    install_jenkins_plugins,
)


hooks = Hooks()


@when('apt.installed.jenkins')
@when_not('jenkins.configured')
def configure_jenkins():
    log("Configuring user for jenkins.", level=DEBUG)
    # Check to see if password provided
    admin_passwd = config('password')
    if not admin_passwd:
        # Generate a random one for security. User can then override using juju
        # set.
        admin_passwd = subprocess.check_output(['pwgen', '-N1', '15'])
        # Decode to UTF-8 to write to file
        admin_passwd = admin_passwd.strip().decode()

    passwd_file = os.path.join(JENKINS_HOME, '.admin_password')
    with open(passwd_file, 'w+') as fd:
        fd.write(admin_passwd)

    os.chmod(passwd_file, 0o600)

    jenkins_uid = pwd.getpwnam('jenkins').pw_uid
    jenkins_gid = grp.getgrnam('jenkins').gr_gid
    nogroup_gid = grp.getgrnam('nogroup').gr_gid

    # Generate Salt and Hash Password for Jenkins
    salt = subprocess.check_output(['pwgen', '-N1', '6']).strip()
    # Re-encode for type compat in hashlib.sha256 below
    admin_passwd = admin_passwd.encode('utf-8')
    csum = hashlib.sha256("%s{%s}".encode('utf-8') %
                          (admin_passwd, salt)).hexdigest()
    salty_password = "%s:%s" % (salt, csum)

    admin_username = config('username')
    admin_user_home = os.path.join(JENKINS_USERS, admin_username)
    if not os.path.isdir(admin_user_home):
        os.makedirs(admin_user_home, 0o700)
        os.chown(JENKINS_USERS, jenkins_uid, nogroup_gid)
        os.chown(admin_user_home, jenkins_uid, nogroup_gid)

    # NOTE: overwriting will destroy any data added by jenkins or via the ui
    admin_user_config = os.path.join(admin_user_home, 'config.xml')
    with open(os.path.join(TEMPLATES_DIR, 'user-config.xml')) as src_fd:
        with open(admin_user_config, 'w') as dst_fd:
            lines = src_fd.readlines()
            for line in lines:
                kvs = {'__USERNAME__': admin_username,
                       '__PASSWORD__': salty_password}

                for key, val in kvs.items():
                    if key in line:
                        line = line.replace(key, val)

                dst_fd.write(line)
                os.chown(admin_user_config, jenkins_uid, nogroup_gid)

    # Only run on first invocation otherwise we blast
    # any configuration changes made
    jenkins_bootstrap_flag = '/var/lib/jenkins/config.bootstrapped'
    if not os.path.exists(jenkins_bootstrap_flag):
        log("Bootstrapping secure initial configuration in Jenkins.",
            level=DEBUG)
        dst = os.path.join(JENKINS_HOME, 'config.xml')
        context = {'master_executors': config('master-executors')}
        render('jenkins-config.xml', dst, context, owner='jenkins',
               group='nogroup')
        # Touch
        with open(jenkins_bootstrap_flag, 'w'):
            pass

    log("Stopping jenkins for plugin update(s)", level=DEBUG)
    service_stop('jenkins')
    install_jenkins_plugins(jenkins_uid, jenkins_gid)
    log("Starting jenkins to pickup configuration changes", level=DEBUG)
    service_start('jenkins')
    open_port(8080)
    status_set('active', 'Jenkins URI: http://%s:8080' % unit_public_ip())
    set_state('jenkins.configured')


@hooks.hook('master-relation-joined')
def master_relation_joined():
    HOSTNAME = unit_get('private-address')
    log("Setting url relation to http://%s:8080" % (HOSTNAME), level=DEBUG)
    relation_set(url="http://%s:8080" % (HOSTNAME))


@hooks.hook('master-relation-changed')
def master_relation_changed():
    password = get_jenkins_password()
    # Once we have the password, export credentials to the slave so it can
    # download slave-agent.jnlp from the master.
    username = config('username')
    relation_set(username=username)
    relation_set(password=password)

    required_settings = ['executors', 'labels', 'slavehost']
    settings = relation_get()
    missing = [s for s in required_settings if s not in settings]
    if missing:
        log("Not all required relation settings received yet (missing=%s) - "
            "skipping" % (', '.join(missing)), level=INFO)
        return

    slavehost = settings['slavehost']
    executors = settings['executors']
    labels = settings['labels']

    # Double check to see if this has happened yet
    if "x%s" % (slavehost) == "x":
        log("Slave host not yet defined - skipping", level=INFO)
        return

    log("Adding slave with hostname %s." % (slavehost), level=DEBUG)
    add_node(slavehost, executors, labels, username, password)
    log("Node slave %s added." % (slavehost), level=DEBUG)


@hooks.hook('master-relation-departed')
def master_relation_departed():
    # Slave hostname is derived from unit name so
    # this is pretty safe
    slavehost = remote_unit()
    log("Deleting slave with hostname %s." % (slavehost), level=DEBUG)
    del_node(slavehost, config('username'), config('password'))


@hooks.hook('master-relation-broken')
def master_relation_broken():
    password = get_jenkins_password()

    for member in relation_ids():
        member = member.replace('/', '-')
        log("Removing node %s from Jenkins master." % (member), level=DEBUG)
        del_node(member, config('username'), password)


@hooks.hook('website-relation-joined')
def website_relation_joined():
    hostname = unit_get('private-address')
    log("Setting website URL to %s:8080" % (hostname), level=DEBUG)
    relation_set(port=8080, hostname=hostname)


@hooks.hook('extension-relation-joined')
def extension_relation_joined():
    log("Updating extension interface with up-to-date data.")
    # Fish out the current zuul address from any relation we have.
    zuul_address = ""
    for rid in relation_ids('zuul'):
        for unit in related_units(rid):
            zuul_address = relation_get(
                rid=rid, unit=unit, attribute='private-address')

    for rid in relation_ids('extension'):
        r_settings = {
            'admin_username': config('username'),
            'admin_password': get_jenkins_password(),
            'jenkins_url': 'http://%s:8080' % unit_get('private-address'),
            'jenkins-admin-user': config('jenkins-admin-user'),
            'jenkins-token': config('jenkins-token')
        }
        relation_set(relation_id=rid, relation_settings=r_settings)
        if zuul_address:
            relation_set(relation_id=rid, zuul_address=zuul_address)


@hooks.hook('extension-relation-changed')
def extension_relation_changed():
    # extension subordinates may request the principle service install
    # specified jenkins plugins
    if relation_get('required_plugins'):
        log("Installing required plugins as requested by jenkins-extension "
            "subordinate.")
        jenkins_uid = pwd.getpwnam('jenkins').pw_uid
        jenkins_gid = grp.getgrnam('jenkins').gr_gid
        install_jenkins_plugins(
            jenkins_uid, jenkins_gid,
            plugins=relation_get('required_plugins'))


ZUUL_CONFIG_SNIPPET = """
<hudson.plugins.gearman.GearmanPluginConfig>
  <enablePlugin>true</enablePlugin>
    <host>{}</host>
    <port>4730</port>
</hudson.plugins.gearman.GearmanPluginConfig>
"""


@hooks.hook('zuul-relation-joined')
def zuul_relation_joined():
    log("Installing and configuring gearman-plugin for Zuul communication")
    # zuul relation requires we install the required plugins and set the
    # address of the remote zuul/gearman service in the plugin setting.
    required_plugins = (
        "credentials ssh-credentials ssh-agent gearman-plugin git-client git")

    # Grab jenkins uid and gid.
    jenkins_uid = pwd.getpwnam('jenkins').pw_uid
    jenkins_gid = grp.getgrnam('jenkins').gr_gid
    log("Installing and configuring gearman-plugin for Zuul communication")
    install_jenkins_plugins(
        jenkins_uid, jenkins_gid, plugins=required_plugins)
    # Generate plugin config with address of remote unit.
    zuul_config = ZUUL_CONFIG_SNIPPET.format(relation_get('private-address'))
    config_path = os.path.join(
        JENKINS_HOME, "hudson.plugins.gearman.GearmanPluginConfig.xml")
    with open(config_path, 'w') as f:
        f.write(zuul_config)

    # Change permission of config file.
    nogroup_gid = grp.getgrnam('nogroup').gr_gid
    os.chown(config_path, jenkins_uid, nogroup_gid)

    # Restart jenkins so changes will take efect.
    service_restart('jenkins')

    # Trigger the extension hook to update it with zuul relation data, if its
    # coded to do so.
    hooks.execute(['extension-relation-joined'])
