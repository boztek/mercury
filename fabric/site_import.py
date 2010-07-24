from fabric.api import *
from fabric.operations import prompt
from os.path import exists
from string import Template
from re import search
from pantheon import *
import pdb
def import_site(site_archive, selected_site = ''):
    '''Import site archive into a Pantheon server'''
    hudson, selected_site, working_dir = _set_env_vars(hudson, selected_site)

    unarchive(site_archive, working_dir)

    site_settings = _get_site(working_dir, selected_site)
    server_settings = get_server_settings()

    _import_database(site_settings, working_dir)
    _setup_site_files(server_settings['webroot'], site_settings['site_name'], working_dir)
    _setup_modules(server_settings['webroot'], site_settings['site_name'])
    _update_settings(server_settings['webroot'], site_settings)
    _set_permissions(server_settings, site_settings['site_name'])

    with cd(server_settings['webroot'] + "sites/"):
        local("ln -s " + site_settings['site_name'] + " " + server_settings['ip'])

    _restart_services(server_settings['distro'])

    #TODO: Write cleanup function
    #TODO: clear solr index (if exists) before using new site
    #

def _set_env_vars(run_from, selected_site):
    # Variables from commandline are always passed as strings. Convert to proper types.
    hudson = False
    if run_from == 'True':
        hudson = True
    if selected_site.strip() == '':
        site = False
    else:
        site = selected_site.stirp()
    return hudson, site, '/tmp/import_site/'

def get_sites(working_dir):
    matched_sites = {}

    sites = get_site_settings(working_dir)
    site_count = len(sites)
    databases = get_database_names(working_dir)
    db_count = len(databases)

    # Single Database
    if db_count == 1:
        # Single Site - Assume site matches database
        if site_count == 1:
            matched_sites[0]['name'] = sites.keys()[0]
            matched_sites[0]['settings'] = sites.values()[0]
            matched_sites[0]['database'] = database.keys()[0]
        # Multiple Sites
        elif sites_count > 1:
            count = 0
            for site_name, settings in sites.iteritems():
                db_name for db_name in databases.values() if db_name[0] == settings['db_name']:
                    matched_sites[count]['name'] = site_name
                    matched_sites[count]['settings'] = settings
                    matched_sites[count]['database'] = db_name
                    count += 1
        else:
            abort("No Drupal Sites Found")
    # Multiple Databases
    elif  database.count() > 1:
    else:
        abort("No Databases Found")

def _get_site(working_dir, site):
    sites = {}
    # Site may have been preselected (in web-interface)
    if site:
        sites[site] = get_database_settings(working_dir + "sites/" + site + "/settings.php")
        return sites
    
    # Get all valid sites
    sites = get_site_settings(working_dir)
    
    if sites.count == 0: abort("No valid settings.php found")
    if sites.count == 1: return sites
    if sites.count > 1:
        # Try to autmatically figure out which site to use first.
        # Test 1: if db name in the dump file comments match the db name in only one settings.php, this is a safe match.
        found = []
        db_dumps = _get_database_dumps()
        if db_dumps.count == 0: abort("No database dumps found")
        if db_dumps.count == 1: 
            db_name = (local(r"awk '/^-- Host:/' " + working_dir + db_dumps[0] \
                          r" | sed 's_.*Host:\s*\(.*\)\s*Database:\s*\(.*\)$_\2_'")).rstrip('\n')
            for name in sites.keys():
                if sites[name]['db_name'] == db_name
                    found[site] = sites[site]
            if found.count == 1: return found
            if found.count == 0: print "WARNING: Database dump does not match any databases defined in settings.php files"
        if db_dumps.count > 1:        
            # TODO: For multiple database support add comparison between dict of sites and dict of databases
            pass
     
        # Automated selection failed. Resort to manual.
    if not hudson:
        pdb.set_trace()
        print "\nMultiple sites found. Please select the site you wish to use:\n"
        count = 0
        for site in sites:
            print "[" + str(count) + "]: " + site['site_name']
            count += 1
        valid = False
        while not valid:
            choice = int(prompt('\nChoose Site: \n', validate=r'^\d{1,2}$'))
            if choice < len(sites) and choice > -1:
                valid = True
        return sites[choice]
    # Script was started by hudson (return list of sites to choose from)
    else:
        with open('/var/lib/hudson/jobs/import_site/workspace/available-sites.txt', 'w') as f:
            for site in sites:
                f.write(site['site_name'] + '\n')
        f.close
        abort("Multiple Sites Found. List stored in available-sites.txt build artifact.")

def _get_database_dumps(working_dir):
    with settings(warn_only=True):
        with cd(working_dir)
            return (local("ls *.sql")).rstrip('\n').split(' ')
    
def _get_drupal_version(working_dir):
    # Test 1: Try to get version from system.module
    version = (local("awk \"/define\(\'VERSION\'/\" " + working_dir + "modules/system/system.module" + "| sed \"s_^.*'\(6\)\.\([0-9]\{1,2\}\)'.*_\\1-\\2_\"")).rstrip('\n')
    if not version:
        # Test 2: Try to get drupal version from system.info
        version = (local("awk '/version/ {if ($3 != \"VERSION\") print $3}' " + working_dir + "modules/system/system.info" + r' | sed "s_^\"\(6\)\.\([0-9]\{1,2\}\)\".*_\1-\2_"')).rstrip('\n')
    if not version:
        # Test 3: Try to get drupal version from Changelog
        version = (local("cat " + working_dir  + "CHANGELOG.txt | grep --max-count=1 Drupal | sed 's/Drupal \([0-9]\)*\.\([0-9]*\).*/\\1-\\2/'")).rstrip('\n')
    if not version:
        abort("Unable to determine Drupal version.")
    else:
        return version

def _get_pressflow_revision(working_dir, drupal_version):
    #TODO: Optimize this (restrict search to revisions within Drupal minor version)
    #TODO: Add check for .bzr metadata
    if exists(working_dir + 'PRESSFLOW.txt'):
        revno = local("cat " + working_dir + "PRESSFLOW.txt").split('.')[2].rstrip('\n')
        return revno
    if exists("/tmp/pf_temp"):
        local("rm -rf /tmp/pf_temp")
    local("bzr branch lp:pressflow/6.x /tmp/pf_temp")
    with cd("/tmp/pf_temp"):
        match = {'num':100000,'revno':0}
        revno = local("bzr revno").rstrip('\n')
        for i in range(int(revno),0,-1):
            local("bzr revert -r" + str(i))
            diff = int(local("diff -rup " + working_dir + " ./ | wc -l"))
            if diff < match['num']:
                match['num'] = diff
                match['revno'] = i
    return str(match['revno'])
        
def _get_branch_and_revision(working_dir):
    #TODO: pressflow.txt  doesn't exists if pulled from bzr
    #TODO: check that it is Drupal V6

    ret = {}
    drupal_version = (_get_drupal_version(working_dir)).rstrip('\n')
    # Check if site uses Pressflow (look in system.module)
    dist = (local("awk \"/\'info\' =>/\" " + working_dir + "modules/system/system.module" + r' | sed "s_^.*Powered by \([a-zA-Z]*\).*_\1_"')).rstrip('\n')
    if dist == 'Drupal':
        ret['branch'] = "lp:drupal/6.x-stable"
        ret['revision'] = "tag:DRUPAL-" + drupal_version 
        ret['type'] = "DRUPAL"
    elif dist == 'Pressflow':
        revision = _get_pressflow_revision(working_dir, drupal_version)
        ret['branch'] = "lp:pressflow/6.x"
        ret['revision'] = revision 
        ret['type'] = "PRESSFLOW"
    else:
        abort("Cannot determine if using Drupal or Pressflow")

    if (ret['revision'] == None) or (ret['revision'] == "tag:DRUPAL-"):
        abort("Unable to determine base Drupal / Pressflow version")

    return ret

def _import_database(db, working_dir):

    db_dump_file = _get_db_dump_name(working_dir)
    #TODO: break drop and create database into own function
    local("mysql -u root -e 'DROP DATABASE IF EXISTS " + db['db_name'] + "'")
    local("mysql -u root -e 'CREATE DATABASE " + db['db_name'] + "'")
    local("mysql -u root -e \"GRANT ALL ON " + db['db_name'] + ".* TO '" + db['db_username'] + "'@'localhost' IDENTIFIED BY '" + db['db_password'] + "';\"")
    local("cat " + db_dump_file + " | grep -v '^INSERT INTO `cache[_a-z]*`' | sed 's/^[)] ENGINE=MyISAM/) ENGINE=InnoDB/' | mysql -u root " + db['db_name'])
    local("rm -f " + db_dump_file)

def _setup_site_files(webroot, site, working_dir):
    #TODO: add large file size sanity check (no commits over 20mb)
    #TODO: sanity check for versions prior to 6.6 (no pressflow branch).
    #TODO: test wildcard in ignore
    #TODO: look into ignoreing files directory
    #TODO: sanity check for conflicts (hacked core)
    #TODO: check if updatedb needs to run. Fabric will return error if it doesn't need to run.
    
    if exists(webroot):
        local('rm -r ' + webroot)

    # Create vanilla drupal/pressflow branch of same version as import site
    version = _get_branch_and_revision(working_dir)

    local("bzr branch -r " + version['revision'] + " " + version['branch'] + " " + webroot)

    # Bring import site up to current Pressflow version
    with cd(webroot):

        # Import site and revert any changes to core
        local("bzr import " + working_dir)
        reverted = local("bzr revert")

        # Cleanup potential issues
        local("rm -f PRESSFLOW.txt")
        #if exists(".bzrignore"):
        #    local('bzr revert .bzrignore')

        # Magic Happens
        #local("bzr add")
        local("bzr commit --unchanged -m 'Automated Commit'")
        local("bzr merge lp:pressflow/6.x")
        local("rm -r ./.bzr")
#local("bzr commit --unchanged -m 'Update to latest Pressflow core'")
        
        # Run update.php. Wrap in warn_only because drush returns failure if it doesn't need to run.
        with settings(warn_only=True):
            local("drush -y --uri=" + site + " updatedb")

    # Save reverted files as hudson build artifacts
    #with open('/var/lib/hudson/jobs/import_site/workspace/reverted.txt', 'w') as f:
    #    f.write(reverted)
    #f.close

def _update_settings(webroot, site_settings):
    #TODO: remove any previously defined $db_url strings rather than relying on ours being last
    slug = Template(local("cat /opt/pantheon/fabric/templates/pantheon.settings.php"))
    slug = slug.safe_substitute(site_settings)
    with open(webroot + "sites/" + site_settings['site_name'] + "/settings.php", 'a') as f:
        f.write(slug)
    f.close

def _get_module_status(site_path):
    #TODO: extend drush so that "drush pm-list" can have xml/json friendly output. Below is temporary stop-gap
    with cd(site_path):
        # Output module status in dictionary friendly format.
        site_modules = local("drush sql-query \"SELECT name, status FROM system WHERE type='module';\" | awk -v sq=\"'\" '{if ($1 != \"name\" && $2 == 1) print \"(\"sq$1sq\", \"sq\"Enabled\"sq\")\"; if ($1 != \"name\" && $2 == 0) print \"(\"sq$1sq\", \"sq\"Disabled\"sq\")\" }'").replace('\n',',')[:-1]
    return dict(eval(site_modules))

def _setup_modules(webroot, site):

    required_modules = {'apachesolr':None, 'apachesolr_search':'Disabled', 'cookie_cache_bypass':'Disabled', 'locale':None, 'memcache_admin':None, 'syslog':None, 'varnish':None}

    # Get module dictionary. Key=Module name, Value=Enabled/Disabled/None
    site_modules = _get_module_status(webroot + "sites/" + site)

    with cd(webroot):
        # If a required module is found, the value is set to site_modules current status (Enabled/Disabled). If not found, value=None.
        for name in required_modules.keys():
            if site_modules.has_key(name):
                required_modules[name] = site_modules[name]

        # Special case: download memcache if memcache_admin doesn't exist, but don't enable memcache_admin.
        if required_modules['memcache_admin'] == None:
            local("drush -y dl memcache")
            required_modules['memcache_admin'] = 'Disabled'
        if required_modules['memcache_admin'] == 'Disabled':
            del(required_modules['memcache_admin'])

        # Special Case: Make sure both apachesolr and apachesolr_search are installed and enabled.
        if required_modules['apachesolr'] == None:
            local("drush -y dl apachesolr")
            required_modules['apachesolr'] = 'Disabled'
            required_modules['apachesolr_search'] = 'Disabled'
        if required_modules['apachesolr'] == 'Disabled':
            local("wget http://solr-php-client.googlecode.com/files/SolrPhpClient.r22.2009-11-09.tgz")
            local("mkdir -p " + webroot + "sites/all/modules/apachesolr/SolrPhpClient/")
            local("tar xzf SolrPhpClient.r22.2009-11-09.tgz -C " + webroot  + "sites/all/modules/apachesolr/")
            with settings(warn_only=True):
                local("drush -y --uri=" + site + " en apachesolr")
            del(required_modules['apachesolr'])
        if required_modules['apachesolr_search'] == 'Disabled':
            with settings(warn_only=True):
                local("drush -y --uri=" + site + " en apachesolr_search")
            del(required_modules['apachesolr_search'])

        # Normal Cases: Download if absent & enable if disabled.
        for module, status in required_modules.iteritems():
            if status == None:
                local("drush -y dl " + module)
                status = 'Disabled' 
            if status == 'Disabled':
                with settings(warn_only=True):
                    local("drush -y --uri=" + site + " en " + module)

    with cd(webroot + "sites/" + site):
        # Set apachesolr variables
        local("drush php-eval \"variable_set('apachesolr_path', '/default');\"")
        local("drush php-eval \"variable_set('apachesolr_port', 8983);\"")
        local("drush php-eval \"variable_set('apachesolr_search_make_default', 1);\"")
        local("drush php-eval \"variable_set('apachesolr_search_spellcheck', TRUE);\"")

        # Set admin/settings/performance variables
        local("drush php-eval \"variable_set('cache', CACHE_EXTERNAL);\"")
        local("drush php-eval \"variable_set('page_cache_max_age', 900);\"")
        local("drush php-eval \"variable_set('block_cache', TRUE);\"")
        local("drush php-eval \"variable_set('page_compression', 0);\"")
        local("drush php-eval \"variable_set('preprocess_js', TRUE);\"")
        local("drush php-eval \"variable_set('preprocess_css', TRUE);\"")

    # Drush will report failure if we try to enable a module that is already enabled.
    # To get around this, we wrap "drush en" in warn_only=True.
    # However, we still want to make sure the modules are enabled (and didn't fail for another reason).
    site_modules = _get_module_status(webroot + "sites/" + site)
    check_modules = ['apachesolr', 'apachesolr_search', 'cookie_cache_bypass', 'locale', 'syslog', 'varnish']
    for module in check_modules:
        if site_modules[module] == 'Disabled':
            print "WARNING: Required module \"" + module + "\" could not be enabled."

def _set_permissions(server_settings, site_name):
    #TODO: make database call to find file dir location for specific site
    # setup ownership and permissions
    local('chown -R ' + server_settings['owner'] + ':' + server_settings['group'] + ' ' + server_settings['webroot'])
    local('chmod 440 ' + server_settings['webroot'] + 'sites/' + site_name + '/settings.php')

    # make sure everything under the 'files' directory has proper perms (770 on dirs, 550 on files)
    with cd(server_settings['webroot'] + 'sites/'):
        local("find . -type d -name files -exec chmod ug=rwx,o= '{}' \;")
        local("find . -name files -type d -exec find '{}' -type f \; | while read FILE; do chmod ug=rw,o= \"$FILE\"; done")
        local("find . -name files -type d -exec find '{}' -type d \; | while read DIR; do chmod ug=rwx,o= \"$DIR\"; done")

def _restart_services(distro):
    if distro == 'ubuntu':
        local('/etc/init.d/apache2 restart')
        local('/etc/init.d/memcached restart')
        local('/etc/init.d/tomcat6 restart')
    elif distro == 'centos':
        local('/etc/init.d/httpd restart')
        local('/etc/init.d/memcached restart')
        local('/etc/init.d/tomcat5 restart')

