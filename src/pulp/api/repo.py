#!/usr/bin/python
#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.


# Python
import logging
import gzip
import os
import traceback

# 3rd Party
import pymongo
import yum.comps
from yum.Errors import CompsException

# Pulp
from grinder.RepoFetch import YumRepoGrinder
from pulp import model
from pulp import upload
from pulp import crontab
from pulp.api import repo_sync
from pulp.api.base import BaseApi
from pulp.api.package import PackageApi
import pulp.api.repo_sync
from pulp.pexceptions import PulpException

log = logging.getLogger(__name__)

class RepoApi(BaseApi):
    """
    API for create/delete/syncing of Repo objects
    """

    def __init__(self, config):
        BaseApi.__init__(self, config)
        self.packageApi = PackageApi(config)
        self.localStoragePath = config.get('paths', 'local_storage')
   
    def _get_indexes(self):
        return ["packages", "packagegroups", "packagegroupcategories"]

    def _get_unique_indexes(self):
        return ["id"]

    def _getcollection(self):
        return self.db.repos

    def _validate_schedule(self, sync_schedule):
        '''
        Verifies the sync schedule is in the correct cron syntax, throwing an exception if
        it is not.
        '''
        if sync_schedule:
            item = crontab.CronItem(sync_schedule + ' null') # CronItem expects a command
            if not item.is_valid():
                raise PulpException('Invalid sync schedule specified [%s]' % sync_schedule)

    def delete(self, **kwargs):
        repo = self.repository(kwargs['id'])
        pulp.api.repo_sync.delete_schedule(self.config, repo)
        self.objectdb.remove(kwargs, safe=True)

    def update(self, repo):
        self._validate_schedule(repo['sync_schedule'])

        self.objectdb.save(repo, safe=True)

        if repo['sync_schedule']:
            pulp.api.repo_sync.update_schedule(self.config, repo)
        else:
            pulp.api.repo_sync.delete_schedule(self.config, repo)

        return repo

    def repositories(self, spec=None, fields=None):
        """
        Return a list of Repositories
        """
        return list(self.objectdb.find(spec=spec, fields=fields))
        
    def repository(self, id, fields=None):
        """
        Return a single Repository object
        """
        return self.objectdb.find_one({'_id': id}, fields=fields)
        
    def packages(self, id, name=None):
        """
        Return list of Package objects in this Repo
        """
        repo = self.repository(id)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % id)
        if (name == None):
            return repo['packages']
        else:
            matches = []
            packages = repo['packages']
            for package in packages.values():
                if (package['name'].index(name) >= 0):
                    matches.append(package)
            return matches
    
    def get_package(self, id, name):
        """
        Return matching Package object in this Repo
        """
        repo = self.repository(id)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % id)
        packages = repo['packages']
        for package in packages.values():
            log.error(package['name'])
            if (package['name'] == name):
                return package
    
    def add_package(self, repoid, packageid):
        """
        Adds the passed in package to this repo
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        package = self.packageApi.package(packageid)
        if (package == None):
            raise PulpException("No Package with id: %s found" % packageid)
        # TODO:  We might want to restrict Packages we add to only
        #        allow 1 NEVRA per repo and require filename to be unique
        self._add_package(repo, package)
        self.update(repo)

    def _add_package(self, repo, p):
        """
        Responsible for properly associating a Package to a Repo
        """
        packages = repo['packages']
        if (packages.has_key(p['id'])):
            # No need to update repo, this Package is already under this repo
            return
        packages[p['id']] = p           

    def remove_package(self, repoid, p):
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        del repo["packages"][p['id']]
        self.update(repo)

    def create_packagegroup(self, repoid, group_id, group_name, description):
        """
        Creates a new packagegroup saved in the referenced repo
        @param repoid:
        @param group_id:
        @param group_name:
        @param description:
        @return packagegroup object
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        if repo["packagegroups"].has_key(group_id):
            raise PulpException("Package group %s already exists in repo %s" % \
                                (group_id, repoid))
        group = pulp.model.PackageGroup(group_id, group_name, description)
        repo["packagegroups"][group_id] = group
        self.update(repo)
        self._update_groups_metadata(repo["id"])
        return group

    def delete_packagegroup(self, repoid, groupid):
        """
        Remove a packagegroup from a repo
        @param repoid:
        @param group_id:
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        if repo['packagegroups'].has_key(groupid):
            del repo['packagegroups'][groupid]
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    def update_packagegroup(self, repoid, pg):
        """
        Save the passed in PackageGroup to this repo
        @param repoid: repo id
        @param pg: packagegroup
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        repo['packagegroups'][pg['id']] = pg
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    def update_packagegroups(self, repoid, pglist):
        """
        Save the list of passed in PackageGroup objects to this repo
        @param repoid: repo id
        @param pg: list of packagegroups
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        for item in pglist:
            repo['packagegroups'][item['id']] = item
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    def packagegroups(self, id):
        """
        Return list of PackageGroup objects in this Repo
        @param id: repo id
        @return: packagegroup or None
        """
        repo = self.repository(id)
        if repo == None:
            raise PulpException("No Repo with id: %s found" % id)
        return repo['packagegroups']
    
    def packagegroup(self, repoid, groupid):
        """
        Return a PackageGroup from this Repo
        @param repoid: repo id
        @param groupid: packagegroup id
        @return: packagegroup or None
        """
        repo = self.repository(repoid)
        if not repo['packagegroups'].has_key(groupid):
            return None
        return repo['packagegroups'][groupid]

    
    def add_package_to_group(self, repoid, groupid, pkg_name, gtype="default"):
        """
        @param repoid: repository id
        @param groupid: group id
        @param pkg_name: package name
        @param gtype: OPTIONAL type of package group,
            example "mandatory", "default", "optional"
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        if not repo["packagegroups"].has_key(groupid):
            raise PulpException("No PackageGroup with id: %s exists in repo %s" \
                                % (groupid, repoid))
        group = repo["packagegroups"][groupid]
        if gtype == "mandatory":
            if pkg_name not in group["mandatory_package_names"]:
                group["mandatory_package_names"].append(pkg_name)
        elif gtype == "conditional":
            raise PulpException("Not Implemented:  support for creating conditional groups")
        elif gtype == "optional":
            if pkg_name not in group["optional_package_names"]:
                group["optional_package_names"].append(pkg_name)
        else:
            if pkg_name not in group["default_package_names"]:
                group["default_package_names"].append(pkg_name)
        self.update(repo)
        self._update_groups_metadata(repo["id"])
        
        
    def delete_package_from_group(self, repoid, groupid, pkg_name, gtype="default"):
        """
        @param repoid: repository id
        @param groupid: group id
        @param pkg_name: package name
        @param gtype: OPTIONAL type of package group,
            example "mandatory", "default", "optional"
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        if not repo["packagegroups"].has_key(groupid):
            raise PulpException("No PackageGroup with id: %s exists in repo %s" \
                                % (groupid, repoid))
        group = repo["packagegroups"][groupid]
        if gtype == "mandatory":
            if pkg_name in group["mandatory_package_names"]:
                group["mandatory_package_names"].remove(pkg_name)
        elif gtype == "conditional":
            raise PulpException("Not Implemented:  support for creating conditional groups")
        elif gtype == "optional":
            if pkg_name in group["optional_package_names"]:
                group["optional_package_names"].remove(pkg_name)
        else:
            if pkg_name in group["default_package_names"]:
                group["default_package_names"].remove(pkg_name)
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    def delete_packagegroupcategory(self, repoid, categoryid):
        """
        Remove a packagegroupcategory from a repo
        """
        repo = self.repository(repoid)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % repoid)
        if repo['packagegroupcategories'].has_key(categoryid):
            del repo['packagegroupcategories'][categoryid]
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    def update_packagegroupcategory(self, repoid, pgc):
        """
        Save the passed in PackageGroupCategory to this repo
        """
        repo = self.repository(repoid)
        if repo == None:
            raise PulpException("No Repo with id: %s found" % repoid)
        repo['packagegroupcategories'][pgc['id']] = pgc
        self.update(repo)
        self._update_groups_metadata(repo["id"])
    
    def update_packagegroupcategories(self, repoid, pgclist):
        """
        Save the list of passed in PackageGroupCategory objects to this repo
        """
        repo = self.repository(repoid)
        if repo == None:
            raise PulpException("No Repo with id: %s found" % repoid)
        for item in pgclist:
            repo['packagegroupcategories'][item['id']] = item
        self.update(repo)
        self._update_groups_metadata(repo["id"])

    def packagegroupcategories(self, id):
        """
        Return list of PackageGroupCategory objects in this Repo
        """
        repo = self.repository(id)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % id)
        return repo['packagegroupcategories']

    def packagegroupcategory(self, repoid, categoryid):
        """
        Return a PackageGroupCategory object from this Repo
        """
        repo = self.repository(repoid)
        if repo == None:
            raise PulpException("No Repo with id: %s found" % repoid)
        if not repo['packagegroupcategories'].has_key(categoryid):
            return None
        return repo['packagegroupcategories'][categoryid]

    def _update_groups_metadata(self, repoid):
        """
        Updates the groups metadata (example: comps.xml) for a given repo
        @param repoid: repo id
        @return: True if metadata was successfully updated, otherwise False
        """
        repo = self.repository(repoid)
        if repo == None:
            raise PulpException("No Repo with id: %s found" % repoid)
        try:
            # If the repomd file is not valid, or if we are missingg
            # a group metadata file, no point in continuing. 
            if not os.path.exists(repo["repomd_xml_path"]):
                log.debug("Skipping update of groups metadata since missing repomd file: '%s'" % \
                          (repo["repomd_xml_path"]))
                return False
            xml = pulp.comps_util.form_comps_xml(repo['packagegroupcategories'],
                repo['packagegroups'])
            if repo["group_xml_path"] == "":
                repo["group_xml_path"] = os.path.dirname(repo["repomd_xml_path"])
                repo["group_xml_path"] = os.path.join(os.path.dirname(repo["repomd_xml_path"]),
                                                      "comps.xml")
                self.update(repo)
            f = open(repo["group_xml_path"], "w")
            f.write(xml.encode("utf-8"))
            f.close()
            if repo["group_gz_xml_path"]:
                gz = gzip.open(repo["group_gz_xml_path"], "wb")
                gz.write(xml.encode("utf-8"))
                gz.close()
            return pulp.comps_util.update_repomd_xml_file(repo["repomd_xml_path"],
                repo["group_xml_path"], repo["group_gz_xml_path"])
        except Exception, e:
            log.debug("_update_groups_metadata exception caught: %s" % (e))
            log.debug("Traceback: %s" % (traceback.format_exc()))
            return False
        
    def create(self, id, name, arch, feed=None, symlinks=False, sync_schedule=None):
        """
        Create a new Repository object and return it
        """
        repo = self.repository(id)
        if (repo):
            raise PulpException("A Repo with id %s already exists" % id)
        self._validate_schedule(sync_schedule)

        r = model.Repo(id, name, arch, feed)
        r['sync_schedule'] = sync_schedule
        r['use_symlinks'] = symlinks
        self.insert(r)

        if sync_schedule:
            pulp.api.repo_sync.update_schedule(self.config, r)

        return r

    def sync(self, id):
        """
        Sync a repo from the URL contained in the feed
        """
        repo = self.repository(id)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % id)
        
        repo_source = repo['source']
        if not repo_source:
            raise PulpException("This repo is not setup for sync. Please add packages using upload.")
        added_packages = repo_sync.sync(self.config, repo, repo_source)
        for p in added_packages:
            self._add_package(repo, p)
        self.update(repo)

    def upload(self, id, pkginfo, pkgstream):
        """
        Store the uploaded package and associate to this repo
        """
        repo = self.repository(id)
        if (repo == None):
            raise PulpException("No Repo with id: %s found" % id)
        pkg_upload = upload.PackageUpload(self.config, repo, pkginfo, pkgstream)
        pkg, repo = pkg_upload.upload()
        self._add_package(repo, pkg)
        self.update(repo)
        log.error("Upload success %s %s" % (pkg['id'], repo['id']))
        return True

    def all_schedules(self):
        '''
        For all repositories, returns a mapping of repository name to sync schedule.
        
        @rtype:  dict
        @return: key - repo name, value - sync schedule
        '''
        repo_api = RepoApi(self.config)
        all_repos = repo_api.repositories()

        result = {}
        for repo in all_repos:
            result[repo['id']] = repo['sync_schedule']

        return result
