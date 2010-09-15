# vim: tabstop=4 shiftwidth=4 softtabstop=4
import copy
import os
import random
import re
import string
import tempfile
import time
import urlparse

from fabric.api import *


def getfrom_url(url):
    download_dir = tempfile.mkdtemp()
    filebase = os.path.basename(url)
    filename = os.path.join(download_dir, filebase)

    curl(url, filename)
    return filename


def curl(url, destination):
    """Fetch a file at a url and save to destination.

    WARNING: Does not do any escaping besides the basic, make sure you
    are giving this sanitized input.

    """
    local('curl "%s" -o "%s"' % (url, destination))


def create_database(database):
    local("mysql -u root -e 'DROP DATABASE IF EXISTS %s'" % (database))
    local("mysql -u root -e 'CREATE DATABASE IF NOT EXISTS' %s" % (database))


def grant_database(grants, database, username, password, hostname = 'localhost'):
    local("mysql -u root -e \"GRANT %s ON %s.* TO '%s'@'%s' IDENTIFIED BY '%s';\"" % (
            grants,
            database,
            username,
            hostname,
            password))

def create_settings_file(site_dir, settings_dict:
    """ Replace settings.php template with values from settings_dict
    site_dir: Full path to site directory. E.g. /var/www/pantheon/dev/sites/default/
    settings_dict: 'username'
                   'password'
                   'database'
                   'memcache_prefix'
    webroot: Path to Drupal installation.

    """
    with open(os.join.path(site_dir, 'settings.php') as f:
        f.write('\n/* Added by Pantheon */\n')
        f.write("include 'pantheon.settings.php';\n")

    slug_template = local("cat /opt/pantheon/fabric/templates/pantheon.settings.php")
    slug = string.Template(slug_template)
    slug = slug.safe_substitute(settings_dict)
    with open(os.join.path(site_dir, 'pantheon.settings.php'), 'w') as f:
        f.write(slug)

def unarchive(archive, destination):
    '''Extract archive to destination directory and remove VCS files'''
    if not os.path.exists(archive):
        abort("Archive file \"" + archive + "\" does not exist.")
            
    if os.path.exists(destination):
        local("rm -rf " + destination)
            
    local("bzr init " + destination)
                
    with cd(destination):
        local("bzr import " + archive)
        with settings(warn_only=True):
            local("rm -r ./.bzr")
            local("rm -r ./.git")
            local("find . -depth -name .svn -exec rm -fr {} \;")
            local("find . -depth -name CVS -exec rm -fr {} \;")


def export_data(webroot, temporary_directory):
    sites = DrupalInstallation(webroot).get_sites()
    with cd(temporary_directory):
        exported = list()
        for site in sites:
            if site.valid:
                # If multiple sites use same db, only export once.
                if site.database.name not in exported:
                    with settings(warn_only=True):
                        result = local("mysqldump --single-transaction --user='%s' --password='%s' --host='%s' %s > %s.sql" % ( \
                                    site.database.username,
                                    site.database.password,
                                    site.database.hostname,
                                    site.database.name,
                                    site.database.name), capture=False)
                    # It is possible that a settings.php defines a 
                    # database/user/pass that doesn't exist or doesn't work.
                    if not result.failed:
                        print "Exported Database: " + site.database.name
                        exported.append(site.database.name)
                        site.database.dump = temporary_directory + "/" + site.database.name + ".sql"
                    else:
                        print "Unable to export database '%s' defined for site '%s'. (incorrect database name, username, and/or password)" % (
                                   site.database.name,
                                   site.name)
    return(sites)


def import_data(sites):
    # Create temporary superuser to perform import operations
    with settings(warn_only=True):
        local("mysql -u root -e \"CREATE USER 'pantheon-admin'@'localhost' IDENTIFIED BY '';\"")
    local("mysql -u root -e \"GRANT ALL PRIVILEGES ON *.* TO 'pantheon-admin'@'localhost' WITH GRANT OPTION;\"")

    for site in sites:
        #TODO: if db username is root, change it. 

        create_database(site.database.name)
        create_database_grant('ALL', site.database.name, site.database.username, site.database.password)
        
        # Strip cache tables, convert MyISAM to InnoDB, and import.
        local("cat %s | grep -v '^INSERT INTO `cache[_a-z]*`' | \
                grep -v '^INSERT INTO `ctools_object_cache`' | \
                grep -v '^INSERT INTO `watchdog`' | \
                grep -v '^INSERT INTO `accesslog`' | \
                grep -v '^USE `' | \
                sed 's/^[)] ENGINE=MyISAM/) ENGINE=InnoDB/' | \
                mysql -u pantheon-admin %s" % \
                  (site.database.dump, site.database.name))
                
    # Cleanup (iterate through sites after import in case multiple sites use same db)
    for site in sites:
        local("rm -f %s" % site.database.dump)
    local("mysql -u pantheon-admin -e \"DROP USER 'pantheon-admin'@'localhost'\"")


def restart_bcfg2():
    local('/etc/init.d/bcfg2-server restart')
    server_running = False
    warn('Waiting for bcfg2 server to start')
    while not server_running:
        with settings(hide('warnings'), warn_only=True):
            server_running = (local('netstat -atn | grep :6789')).rstrip('\n')
        time.sleep(5)


class DrupalInstallation:

    def __init__(self, location):
        self.location = location

    def init_drupal_data(self):
        self.version = self.get_drupal_version()
        self.platform =  self.get_drupal_platform()
        self.set_branch_and_revision()
        self.sites = self.get_sites()

    def get_sites(self):
        sites = []
        site_names = self.get_site_names()
        for name in site_names:
            sites.append(self.get_site_data(name))
        return sites

    def get_site_names(self):
        names = list()
        # Get all settings.php files
        with cd(self.location):
            settings_files = (local('find sites/ -name settings.php -type f')).rstrip('\n')
        # Single site
        if '\n' not in settings_files:
            names.append((re.search(r'^.*sites/(.*)/settings.php', settings_files)).group(1))
        # Multiple sites
        else:
            settings_files = settings_files.split('\n')
            for sfile in settings_files:
                names.append((re.search(r'^.*sites/(.*)/settings.php',sfile)).group(1))
        return names

    def get_site_data(self, name):
        site = DrupalSite(name, self.location)
        site.database.get_settings(self.location, site.name)
        site.valid = site.database.is_valid()
        return site
    
    def get_drupal_platform(self):
        return ((local("awk \"/\'info\' =>/\" " + self.location + "modules/system/system.module" + \
                r' | sed "s_^.*Powered by \([a-zA-Z]*\).*_\1_"')).rstrip('\n').upper())

    def get_drupal_version(self):
        return ((local("awk \"/define\(\'VERSION\'/\" " + self.location + "modules/system/system.module" + \
                "| sed \"s_^.*'\(6\)\.\([0-9]\{1,2\}\)'.*_\\1-\\2_\"")).rstrip('\n'))

    def get_pressflow_revision(self):
        #TODO: Optimize this (restrict search to revisions within Drupal minor version)
        #TODO: Add check for Bazaar or git metadata
        temporary_directory = tempfile.mkdtemp()
        local("git clone git://gitorious.org/pressflow/6.git " + temporary_directory)
        with cd(temporary_directory):
            match = {'difference': None, 'commit': None}
            commits = local("git log | grep '^commit' | sed 's/^commit //'").split('\n')
            for commit in commits:
                if len(commit) > 1:
                    local("git reset --hard " + commit)
                    difference = int(local("diff -rup " + self.location + " ./ | wc -l"))
                    print("Commit " + commit + " shows difference of " + str(difference))
                    if match['commit'] == None or difference < match['difference']:
                        match['difference'] = difference
                        match['commit'] = commit
        return match['commit']

    def set_branch_and_revision(self):
        if self.platform == "DRUPAL":
            self.branch = "git://gitorious.org/drupal/6.git"
            self.revision = "DRUPAL-" + self.version
        elif self.platform == "PRESSFLOW":
            self.branch = "git://gitorious.org/pressflow/6.git"
            self.revision = self.get_pressflow_revision()

    def valid_site_count(self):
        return len([site for site in self.sites if site.valid])


class DrupalSite:

    def __init__(self, name = '', webroot = ''):
        self.name = name
        self.webroot = webroot
        self.database = self.DrupalDB()
        self.file_location = ''
        self.valid = False

    def get_file_location(self, webroot = None):
        if not webroot:
            webroot = self.webroot
        with cd(self.webroot):
            return (local("drush --uri=%s variable-get file_directory_path | \
                grep 'file_directory_path: \"' | \
                sed 's/^file_directory_path: \"\(.*\)\".*/\\1/'" % self.name)).rstrip('\n')

    def set_site_perms(self, webroot = None):
        if not webroot:
            webroot = self.webroot
        # Settings.php Permissions
        with cd(webroot + "sites/" + self.name):
            local("chmod 440 settings.php")
        # File directory permissions (770 on all child directories, 660 on all files)
        with cd(webroot + self.get_file_location(webroot)):
            local("chmod 770 .")
            local("find . -type d -exec find '{}' -type f \; | while read FILE; do chmod 660 \"$FILE\"; done")
            local("find . -type d -exec find '{}' -type d \; | while read DIR; do chmod 770 \"$DIR\"; done")


    def get_settings_dict(self, project):
       ret = {'username':self.database.username,
               'password':self.database.password,
               'database':self.database.name,
               'memcache_prefix':self._get_memcache_prefix(project)}
       return ret

    def _get_memcache_prefix(self, name):
        """Return name + 8 character random string (ascii + digits)
        name: identifier for memcahe prefix. Generally 'project' name is used.

        """
        return name + ''.join(["%s" % random.choice(string.ascii_letters + string.digits) for i in range(8)])

    def set_variables(self, variables = dict()):
        with cd(self.webroot):
            for key, value in variables.iteritems():
                # normalize strings and bools
                if isinstance(value, str):
                    value = "'" + value + "'"
                if isinstance(value, bool):
                    if value == True:
                        value = 'TRUE'
                    elif value == False:
                        value= 'FALSE'
                local("drush --uri=%s php-eval \"variable_set('%s',%s);\"" % (self.name, key, value))

    def drush(self, cmd, options = [' ']):
        with cd(self.webroot):
            with settings(warn_only=True):
                for option in options:
                    local("drush -y --uri=%s %s %s" % (self.name, cmd, option))

    def get_safe_name(self):
        ''' Replace invalid filename/database chars with underscores '''
        return self.name.translate(string.maketrans('\/?%*:|"<>.-','____________'))

    class DrupalDB:

        def __init__(self):
            self.name = ''
            self.username = ''
            self.password = ''
            self.hostname = ''

        def get_settings(self, webroot, site_name):
            settings_file = webroot + "sites/" + site_name + "/settings.php"
            url = (local("awk '/^\$db_url = /' " + settings_file + \
                  " | sed 's/^.*'\\''\([a-z]*\):\(.*\)'\\''.*$/\\2/'")).rstrip('\n')

            # Use last db connection string
            if '\n' not in url:
                url = urlparse.urlparse(url)
            else:
                url = url.split('\n')
                url = urlparse.urlparse(url[len(url)-1])

            if url.password == None:
                self.password = ''
            else:
                self.password = url.password

            self.username = url.username
            self.name = url.path[1:].replace('\n','')
            self.hostname = url.hostname

        def is_valid(self):
            if self.name == None:
                return False
            if self.name == "databasename" \
                    and self.username == "username" \
                    and self.password == "password" \
                    and self.hostname == "localhost": 
                return False
            return True

class PantheonServer:

    def __init__(self):
        # Ubuntu / Debian
        if os.path.exists('/etc/debian_version'):
            self.distro = 'ubuntu'
            self.group = 'www-data'
            self.mysql = 'mysql'
            self.owner = 'root'
            self.tomcat_owner = 'tomcat6'
            self.tomcat_version = '6'
            self.webroot = '/var/www/'
            self.ftproot = '/srv/ftp/pantheon/'
            self.vhost_dir = '/etc/apache2/sites-available/'
        # Centos
        elif os.path.exists('/etc/redhat-release'):
            self.distro = 'centos'
            self.group = 'apache'
            self.mysql = 'mysqld'
            self.owner = 'root'
            self.tomcat_owner = 'tomcat'
            self.tomcat_version = '5'
            self.webroot = '/var/www/html/'
            self.ftproot = '/var/ftp/pantheon/'
            self.vhost_dir = '/etc/httpd/conf/vhosts/'
        self.ip = (local('hostname --ip-address')).rstrip('\n')
        if os.path.exists("/usr/local/bin/ec2-metadata"):
            self.hostname = local('/usr/local/bin/ec2-metadata -p | sed "s/public-hostname: //"').rstrip('\n')
        else:
            self.hostname = local('hostname').rstrip('\n')

    def update_packages(self):
        if (self.distro == "centos"):
            local('yum clean all')
            local('yum -y update')
        else:
            local('apt-get -y update')
            local('apt-get -y dist-upgrade')

    def restart_services(self):
        if self.distro == 'ubuntu':
            local('/etc/init.d/apache2 restart')
            local('/etc/init.d/memcached restart')
            local('/etc/init.d/tomcat6 restart')
            local('/etc/init.d/varnish restart')
            local('/etc/init.d/mysql restart')
        elif self.distro == 'centos':
            local('/etc/init.d/httpd restart')
            local('/etc/init.d/memcached restart')
            local('/etc/init.d/tomcat5 restart')
            local('/etc/init.d/varnish restart')
            local('/etc/init.d/mysqld restart')

    def setup_iptables(self, file):
        local('/sbin/iptables-restore < ' + file)
        local('/sbin/iptables-save > /etc/iptables.rules')

    def create_vhost(self, vhost_dict):
        vhost_template = local("cat /etc/pantheon/templates/vhost.%s.template" % self.distro)
        template = string.Template(vhost_template)
        template = template.safe_substitute(vhost_dict)

        vhost = '%s_%s_%s' % (
                vhost_dict.get('project'),
                vhost_dict.get('environment'),
                vhost_dict.get('site'))

        with open(os.path.join(self.vhost_dir, vhost), 'w') as f:
            f.write(template)
  

    def create_solr_index(self, name):
        """ Create Solr index and tell Tomcat where it is located. 
        name: Index directory. Standard format is: project_environment_site

        """
        # Setup indexes
        solr_dir = '/var/solr/' + name
        if os.path.exists(solr_dir):
            local("rm -rf %s" % solr_dir)
        solr_template = '/opt/pantheon/fabric/templates/solr/'
        local("cp -R %s %s" % (solr_template, solr_dir))
        local('chown -R %s:%s %s' % (
                self.tomcat_owner,
                self.tomcat_owner,
                solr_dir))

        # Tell Tomcat where the indexes are located
        tomcat_template = local("cat /opt/pantheon/fabric/templates/tomcat_solr_home.xml")
        template = string.Template(tomcat_template)
        template = template.safe_substitute({'solr_path':name})
        tomcat_dir = "/etc/tomcat%s/Catalina/localhost/%s.xml" % (
                self.tomcat_version,
                name)

        with open(tomcat_dir, 'w') as f:
            f.write(template)

class SiteImport:
    
    def __init__(self, location, webroot, project, environment):
        if os.path.exists(location):
            self.location = location
            self.project = project
            self.environment = environment
            self.destination = webroot + project + '/' + environment + '/'
            self.drupal = DrupalInstallation(location)
            self.drupal.init_drupal_data()
            self.sql_dumps = self.get_sql_files()
            self.sites = self.get_matched_sites()
    
    def get_sql_files(self):
        databases = list()
        with cd(self.drupal.location):
            with settings(warn_only=True):
                sql_dumps = (local("find . -maxdepth 1 -type f | grep '\.sql'")).replace('./','').rstrip('\n')
                if not sql_dumps:
                    abort("No .sql files found")
        # One database file
        if '\n' not in sql_dumps:
            databases.append(self.SQLDump(self.drupal.location + sql_dumps))
        # Multiple database file
        else:
            sql_dumps = sql_dumps.split('\n')
            for dump in sql_dumps:
                databases.append(self.SQLDump(self.drupal.location + dump))
        return databases

    def get_matched_sites(self):
        matches = list()
        # If one site and one database, don't test anything just assume they match.
        if self.drupal.valid_site_count() == 1 and self.database_count() == 1:
            for site in self.drupal.sites:
                if site.valid:
                    match = copy.deepcopy(site)
                    match.database.dump = self.sql_dumps[0].sql_file
                    matches.append(match)
        # More than one site and/or database
        else:
            for site in self.drupal.sites:
                if site.valid:
                    for dump in self.sql_dumps:
                        if site.database.name == dump.database_name:
                            match = copy.deepcopy(site)
                            match.database.dump = dump.sql_file
                            matches.append(match)

        # Set site webroot to new destination (webroot + project + environment)
        for match in matches:
            match.webroot = self.destination

        return matches

    def database_count(self):
        return len(self.sql_dumps)

    class SQLDump:

        def __init__(self, sql_file):
            self.sql_file = sql_file
            self.database_name = self.get_database_name(sql_file)

        def get_database_name(self, sql_file):
            # Check for 'USE' statement
            name = (local("grep '^USE `' " + sql_file + r" | sed 's/^.*`\(.*\)`;/\1/'")).rstrip('\n')
            # If multiple databases defined in dump file, abort.
            if '\n' in name:
                abort("Multiple databases found in: " + sql_file)
            # Check dump file comments for database name
            elif not name:
                name = (local(r"awk '/^-- Host:/' " + sql_file \
                    + r" | sed 's_.*Host:\s*\(.*\)\s*Database:\s*\(.*\)$_\2_'")).rstrip('\n')
            # If multiple databases defined in dump file, abort.
            if '\n' in name:
                abort("Multiple databases found in: " + sql_file)
            return name

