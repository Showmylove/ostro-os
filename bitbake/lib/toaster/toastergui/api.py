#
# BitBake Toaster Implementation
#
# Copyright (C) 2016        Intel Corporation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# Please run flake8 on this file before sending patches

import re
import logging

from orm.models import Project, ProjectTarget, Build, Layer_Version
from orm.models import LayerVersionDependency, LayerSource, ProjectLayer
from orm.models import Recipe, CustomImageRecipe, CustomImagePackage
from orm.models import Layer, Target, Package, Package_Dependency
from bldcontrol.models import BuildRequest
from bldcontrol import bbcontroller

from django.http import HttpResponse, JsonResponse
from django.views.generic import View
from django.core.urlresolvers import reverse
from django.utils import timezone
from django.db.models import Q, F
from django.db import Error
from toastergui.templatetags.projecttags import json, sectohms, get_tasks
from toastergui.templatetags.projecttags import filtered_filesizeformat

logger = logging.getLogger("toaster")


def error_response(error):
    return JsonResponse({"error": error})


class XhrBuildRequest(View):

    def get(self, request, *args, **kwargs):
        return HttpResponse()

    def post(self, request, *args, **kwargs):
        """
          Build control

          Entry point: /xhr_buildrequest/<project_id>
          Method: POST

          Args:
              id: id of build to change
              buildCancel = build_request_id ...
              buildDelete = id ...
              targets = recipe_name ...

          Returns:
              {"error": "ok"}
            or
              {"error": <error message>}
        """

        project = Project.objects.get(pk=kwargs['pid'])

        if 'buildCancel' in request.POST:
            for i in request.POST['buildCancel'].strip().split(" "):
                try:
                    br = BuildRequest.objects.get(project=project, pk=i)

                    try:
                        bbctrl = bbcontroller.BitbakeController(br.environment)
                        bbctrl.forceShutDown()
                    except:
                        # We catch a bunch of exceptions here because
                        # this is where the server has not had time to start up
                        # and the build request or build is in transit between
                        # processes.
                        # We can safely just set the build as cancelled
                        # already as it never got started
                        build = br.build
                        build.outcome = Build.CANCELLED
                        build.save()

                    # We now hand over to the buildinfohelper to update the
                    # build state once we've finished cancelling
                    br.state = BuildRequest.REQ_CANCELLING
                    br.save()

                except BuildRequest.DoesNotExist:
                    return error_response('No such build request id %s' % i)

            return error_response('ok')

        if 'buildDelete' in request.POST:
            for i in request.POST['buildDelete'].strip().split(" "):
                try:
                    BuildRequest.objects.select_for_update().get(
                        project=project,
                        pk=i,
                        state__lte=BuildRequest.REQ_DELETED).delete()

                except BuildRequest.DoesNotExist:
                    pass
            return error_response("ok")

        if 'targets' in request.POST:
            ProjectTarget.objects.filter(project=project).delete()
            s = str(request.POST['targets'])
            for t in re.sub(r'[;%|"]', '', s).split(" "):
                if ":" in t:
                    target, task = t.split(":")
                else:
                    target = t
                    task = ""
                ProjectTarget.objects.create(project=project,
                                             target=target,
                                             task=task)
            project.schedule_build()

            return error_response('ok')

        response = HttpResponse()
        response.status_code = 500
        return response


class XhrLayer(View):
    """ Get and Update Layer information """

    def post(self, request, *args, **kwargs):
        """
          Update a layer

          Entry point: /xhr_layer/<layerversion_id>
          Method: POST

          Args:
              vcs_url, dirpath, commit, up_branch, summary, description,
              local_source_dir

              add_dep = append a layerversion_id as a dependency
              rm_dep = remove a layerversion_id as a depedency
          Returns:
              {"error": "ok"}
            or
              {"error": <error message>}
        """

        try:
            # We currently only allow Imported layers to be edited
            layer_version = Layer_Version.objects.get(
                id=kwargs['layerversion_id'],
                project=kwargs['pid'],
                layer_source=LayerSource.TYPE_IMPORTED)

        except Layer_Version.DoesNotExist:
            return error_response("Cannot find imported layer to update")

        if "vcs_url" in request.POST:
            layer_version.layer.vcs_url = request.POST["vcs_url"]
        if "dirpath" in request.POST:
            layer_version.dirpath = request.POST["dirpath"]
        if "commit" in request.POST:
            layer_version.commit = request.POST["commit"]
            layer_version.branch = request.POST["commit"]
        if "summary" in request.POST:
            layer_version.layer.summary = request.POST["summary"]
        if "description" in request.POST:
            layer_version.layer.description = request.POST["description"]
        if "local_source_dir" in request.POST:
            layer_version.layer.local_source_dir = \
                request.POST["local_source_dir"]

        if "add_dep" in request.POST:
            lvd = LayerVersionDependency(
                layer_version=layer_version,
                depends_on_id=request.POST["add_dep"])
            lvd.save()

        if "rm_dep" in request.POST:
            rm_dep = LayerVersionDependency.objects.get(
                layer_version=layer_version,
                depends_on_id=request.POST["rm_dep"])
            rm_dep.delete()

        try:
            layer_version.layer.save()
            layer_version.save()
        except Exception as e:
            return error_response("Could not update layer version entry: %s"
                                  % e)

        return JsonResponse({"error": "ok"})

    def delete(self, request, *args, **kwargs):
        try:
            # We currently only allow Imported layers to be deleted
            layer_version = Layer_Version.objects.get(
                id=kwargs['layerversion_id'],
                project=kwargs['pid'],
                layer_source=LayerSource.TYPE_IMPORTED)
        except Layer_Version.DoesNotExist:
            return error_response("Cannot find imported layer to delete")

        try:
            ProjectLayer.objects.get(project=kwargs['pid'],
                                     layercommit=layer_version).delete()
        except ProjectLayer.DoesNotExist:
            pass

        layer_version.layer.delete()
        layer_version.delete()

        return JsonResponse({
            "error": "ok",
            "redirect": reverse('project', args=(kwargs['pid'],))
        })


class MostRecentBuildsView(View):
    def _was_yesterday_or_earlier(self, completed_on):
        now = timezone.now()
        delta = now - completed_on

        if delta.days >= 1:
            return True

        return False

    def get(self, request, *args, **kwargs):
        """
        Returns a list of builds in JSON format.
        """
        project = None

        project_id = request.GET.get('project_id', None)
        if project_id:
            try:
                project = Project.objects.get(pk=project_id)
            except:
                # if project lookup fails, assume no project
                pass

        recent_build_objs = Build.get_recent(project)
        recent_builds = []

        for build_obj in recent_build_objs:
            dashboard_url = reverse('builddashboard', args=(build_obj.pk,))
            buildtime_url = reverse('buildtime', args=(build_obj.pk,))
            rebuild_url = \
                reverse('xhr_buildrequest', args=(build_obj.project.pk,))
            cancel_url = \
                reverse('xhr_buildrequest', args=(build_obj.project.pk,))

            build = {}
            build['id'] = build_obj.pk
            build['dashboard_url'] = dashboard_url

            buildrequest_id = None
            if hasattr(build_obj, 'buildrequest'):
                buildrequest_id = build_obj.buildrequest.pk
            build['buildrequest_id'] = buildrequest_id

            build['recipes_parsed_percentage'] = \
                int((build_obj.recipes_parsed /
                     build_obj.recipes_to_parse) * 100)

            tasks_complete_percentage = 0
            if build_obj.outcome in (Build.SUCCEEDED, Build.FAILED):
                tasks_complete_percentage = 100
            elif build_obj.outcome == Build.IN_PROGRESS:
                tasks_complete_percentage = build_obj.completeper()
            build['tasks_complete_percentage'] = tasks_complete_percentage

            build['state'] = build_obj.get_state()

            build['errors'] = build_obj.errors.count()
            build['dashboard_errors_url'] = dashboard_url + '#errors'

            build['warnings'] = build_obj.warnings.count()
            build['dashboard_warnings_url'] = dashboard_url + '#warnings'

            build['buildtime'] = sectohms(build_obj.timespent_seconds)
            build['buildtime_url'] = buildtime_url

            build['rebuild_url'] = rebuild_url
            build['cancel_url'] = cancel_url

            build['is_default_project_build'] = build_obj.project.is_default

            build['build_targets_json'] = \
                json(get_tasks(build_obj.target_set.all()))

            # convert completed_on time to user's timezone
            completed_on = timezone.localtime(build_obj.completed_on)

            completed_on_template = '%H:%M'
            if self._was_yesterday_or_earlier(completed_on):
                completed_on_template = '%d/%m/%Y ' + completed_on_template
            build['completed_on'] = completed_on.strftime(
                completed_on_template)

            targets = []
            target_objs = build_obj.get_sorted_target_list()
            for target_obj in target_objs:
                if target_obj.task:
                    targets.append(target_obj.target + ':' + target_obj.task)
                else:
                    targets.append(target_obj.target)
            build['targets'] = ' '.join(targets)

            # abbreviated form of the full target list
            abbreviated_targets = ''
            num_targets = len(targets)
            if num_targets > 0:
                abbreviated_targets = targets[0]
            if num_targets > 1:
                abbreviated_targets += (' +%s' % (num_targets - 1))
            build['targets_abbreviated'] = abbreviated_targets

            recent_builds.append(build)

        return JsonResponse(recent_builds, safe=False)


class XhrCustomRecipe(View):
    """ Create a custom image recipe """

    def post(self, request, *args, **kwargs):
        """
        Custom image recipe REST API

        Entry point: /xhr_customrecipe/
        Method: POST

        Args:
            name: name of custom recipe to create
            project: target project id of orm.models.Project
            base: base recipe id of orm.models.Recipe

        Returns:
            {"error": "ok",
             "url": <url of the created recipe>}
            or
            {"error": <error message>}
        """
        # check if request has all required parameters
        for param in ('name', 'project', 'base'):
            if param not in request.POST:
                return error_response("Missing parameter '%s'" % param)

        # get project and baserecipe objects
        params = {}
        for name, model in [("project", Project),
                            ("base", Recipe)]:
            value = request.POST[name]
            try:
                params[name] = model.objects.get(id=value)
            except model.DoesNotExist:
                return error_response("Invalid %s id %s" % (name, value))

        # create custom recipe
        try:

            # Only allowed chars in name are a-z, 0-9 and -
            if re.search(r'[^a-z|0-9|-]', request.POST["name"]):
                return error_response("invalid-name")

            custom_images = CustomImageRecipe.objects.all()

            # Are there any recipes with this name already in our project?
            existing_image_recipes_in_project = custom_images.filter(
                name=request.POST["name"], project=params["project"])

            if existing_image_recipes_in_project.count() > 0:
                return error_response("image-already-exists")

            # Are there any recipes with this name which aren't custom
            # image recipes?
            custom_image_ids = custom_images.values_list('id', flat=True)
            existing_non_image_recipes = Recipe.objects.filter(
                Q(name=request.POST["name"]) & ~Q(pk__in=custom_image_ids)
            )

            if existing_non_image_recipes.count() > 0:
                return error_response("recipe-already-exists")

            # create layer 'Custom layer' and verion if needed
            layer = Layer.objects.get_or_create(
                name=CustomImageRecipe.LAYER_NAME,
                summary="Layer for custom recipes",
                vcs_url="file:///toaster_created_layer")[0]

            # Check if we have a layer version already
            # We don't use get_or_create here because the dirpath will change
            # and is a required field
            lver = Layer_Version.objects.filter(Q(project=params['project']) &
                                                Q(layer=layer) &
                                                Q(build=None)).last()
            if lver is None:
                lver, created = Layer_Version.objects.get_or_create(
                    project=params['project'],
                    layer=layer,
                    dirpath="toaster_created_layer")

            # Add a dependency on our layer to the base recipe's layer
            LayerVersionDependency.objects.get_or_create(
                layer_version=lver,
                depends_on=params["base"].layer_version)

            # Add it to our current project if needed
            ProjectLayer.objects.get_or_create(project=params['project'],
                                               layercommit=lver,
                                               optional=False)

            # Create the actual recipe
            recipe, created = CustomImageRecipe.objects.get_or_create(
                name=request.POST["name"],
                base_recipe=params["base"],
                project=params["project"],
                layer_version=lver,
                is_image=True)

            # If we created the object then setup these fields. They may get
            # overwritten later on and cause the get_or_create to create a
            # duplicate if they've changed.
            if created:
                recipe.file_path = request.POST["name"]
                recipe.license = "MIT"
                recipe.version = "0.1"
                recipe.save()

        except Error as err:
            return error_response("Can't create custom recipe: %s" % err)

        # Find the package list from the last build of this recipe/target
        target = Target.objects.filter(Q(build__outcome=Build.SUCCEEDED) &
                                       Q(build__project=params['project']) &
                                       (Q(target=params['base'].name) |
                                        Q(target=recipe.name))).last()
        if target:
            # Copy in every package
            # We don't want these packages to be linked to anything because
            # that underlying data may change e.g. delete a build
            for tpackage in target.target_installed_package_set.all():
                try:
                    built_package = tpackage.package
                    # The package had no recipe information so is a ghost
                    # package skip it
                    if built_package.recipe is None:
                        continue

                    config_package = CustomImagePackage.objects.get(
                        name=built_package.name)

                    recipe.includes_set.add(config_package)
                except Exception as e:
                    logger.warning("Error adding package %s %s" %
                                   (tpackage.package.name, e))
                    pass

        return JsonResponse(
            {"error": "ok",
             "packages": recipe.get_all_packages().count(),
             "url": reverse('customrecipe', args=(params['project'].pk,
                                                  recipe.id))})


class XhrCustomRecipeId(View):
    """
    Set of ReST API processors working with recipe id.

    Entry point: /xhr_customrecipe/<recipe_id>

    Methods:
        GET - Get details of custom image recipe
        DELETE - Delete custom image recipe

    Returns:
        GET:
        {"error": "ok",
        "info": dictionary of field name -> value pairs
        of the CustomImageRecipe model}
        DELETE:
        {"error": "ok"}
          or
        {"error": <error message>}
    """
    @staticmethod
    def _get_ci_recipe(recipe_id):
        """ Get Custom Image recipe or return an error response"""
        try:
            custom_recipe = \
                    CustomImageRecipe.objects.get(pk=recipe_id)
            return custom_recipe, None

        except CustomImageRecipe.DoesNotExist:
            return None, error_response("Custom recipe with id=%s "
                                        "not found" % recipe_id)

    def get(self, request, *args, **kwargs):
        custom_recipe, error = self._get_ci_recipe(kwargs['recipe_id'])
        if error:
            return error

        if request.method == 'GET':
            info = {"id": custom_recipe.id,
                    "name": custom_recipe.name,
                    "base_recipe_id": custom_recipe.base_recipe.id,
                    "project_id": custom_recipe.project.id}

            return JsonResponse({"error": "ok", "info": info})

    def delete(self, request, *args, **kwargs):
        custom_recipe, error = self._get_ci_recipe(kwargs['recipe_id'])
        if error:
            return error

        custom_recipe.delete()
        return JsonResponse({"error": "ok"})


class XhrCustomRecipePackages(View):
    """
    ReST API to add/remove packages to/from custom recipe.

    Entry point: /xhr_customrecipe/<recipe_id>/packages/<package_id>
    Methods:
         PUT - Add package to the recipe
         DELETE - Delete package from the recipe
         GET - Get package information

     Returns:
         {"error": "ok"}
          or
          {"error": <error message>}
    """
    @staticmethod
    def _get_package(package_id):
        try:
            package = CustomImagePackage.objects.get(pk=package_id)
            return package, None
        except Package.DoesNotExist:
            return None, error_response("Package with id=%s "
                                        "not found" % package_id)

    def _traverse_dependents(self, next_package_id,
                             rev_deps, all_current_packages, tree_level=0):
        """
        Recurse through reverse dependency tree for next_package_id.
        Limit the reverse dependency search to packages not already scanned,
        that is, not already in rev_deps.
        Limit the scan to a depth (tree_level) not exceeding the count of
        all packages in the custom image, and if that depth is exceeded
        return False, pop out of the recursion, and write a warning
        to the log, but this is unlikely, suggesting a dependency loop
        not caught by bitbake.
        On return, the input/output arg rev_deps is appended with queryset
        dictionary elements, annotated for use in the customimage template.
        The list has unsorted, but unique elements.
        """
        max_dependency_tree_depth = all_current_packages.count()
        if tree_level >= max_dependency_tree_depth:
            logger.warning(
                "The number of reverse dependencies "
                "for this package exceeds " + max_dependency_tree_depth +
                " and the remaining reverse dependencies will not be removed")
            return True

        package = CustomImagePackage.objects.get(id=next_package_id)
        dependents = \
            package.package_dependencies_target.annotate(
                name=F('package__name'),
                pk=F('package__pk'),
                size=F('package__size'),
            ).values("name", "pk", "size").exclude(
                ~Q(pk__in=all_current_packages)
            )

        for pkg in dependents:
            if pkg in rev_deps:
                # already seen, skip dependent search
                continue

            rev_deps.append(pkg)
            if (self._traverse_dependents(pkg["pk"], rev_deps,
                                          all_current_packages,
                                          tree_level+1)):
                return True

        return False

    def _get_all_dependents(self, package_id, all_current_packages):
        """
        Returns sorted list of recursive reverse dependencies for package_id,
        as a list of dictionary items, by recursing through dependency
        relationships.
        """
        rev_deps = []
        self._traverse_dependents(package_id, rev_deps, all_current_packages)
        rev_deps = sorted(rev_deps, key=lambda x: x["name"])
        return rev_deps

    def get(self, request, *args, **kwargs):
        recipe, error = XhrCustomRecipeId._get_ci_recipe(
            kwargs['recipe_id'])
        if error:
            return error

        # If no package_id then list all the current packages
        if not kwargs['package_id']:
            total_size = 0
            packages = recipe.get_all_packages().values("id",
                                                        "name",
                                                        "version",
                                                        "size")
            for package in packages:
                package['size_formatted'] = \
                    filtered_filesizeformat(package['size'])
                total_size += package['size']

            return JsonResponse({"error": "ok",
                                 "packages": list(packages),
                                 "total": len(packages),
                                 "total_size": total_size,
                                 "total_size_formatted":
                                 filtered_filesizeformat(total_size)})
        else:
            package, error = XhrCustomRecipePackages._get_package(
                kwargs['package_id'])
            if error:
                return error

            all_current_packages = recipe.get_all_packages()

            # Dependencies for package which aren't satisfied by the
            # current packages in the custom image recipe
            deps = package.package_dependencies_source.for_target_or_none(
                recipe.name)['packages'].annotate(
                name=F('depends_on__name'),
                pk=F('depends_on__pk'),
                size=F('depends_on__size'),
                ).values("name", "pk", "size").filter(
                # There are two depends types we don't know why
                (Q(dep_type=Package_Dependency.TYPE_TRDEPENDS) |
                 Q(dep_type=Package_Dependency.TYPE_RDEPENDS)) &
                ~Q(pk__in=all_current_packages)
                )

            # Reverse dependencies which are needed by packages that are
            # in the image. Recursive search providing all dependents,
            # not just immediate dependents.
            reverse_deps = self._get_all_dependents(kwargs['package_id'],
                                                    all_current_packages)
            total_size_deps = 0
            total_size_reverse_deps = 0

            for dep in deps:
                dep['size_formatted'] = \
                    filtered_filesizeformat(dep['size'])
                total_size_deps += dep['size']

            for dep in reverse_deps:
                dep['size_formatted'] = \
                    filtered_filesizeformat(dep['size'])
                total_size_reverse_deps += dep['size']

            return JsonResponse(
                {"error": "ok",
                 "id": package.pk,
                 "name": package.name,
                 "version": package.version,
                 "unsatisfied_dependencies": list(deps),
                 "unsatisfied_dependencies_size": total_size_deps,
                 "unsatisfied_dependencies_size_formatted":
                 filtered_filesizeformat(total_size_deps),
                 "reverse_dependencies": list(reverse_deps),
                 "reverse_dependencies_size": total_size_reverse_deps,
                 "reverse_dependencies_size_formatted":
                 filtered_filesizeformat(total_size_reverse_deps)})

    def put(self, request, *args, **kwargs):
        recipe, error = XhrCustomRecipeId._get_ci_recipe(kwargs['recipe_id'])
        package, error = self._get_package(kwargs['package_id'])
        if error:
            return error

        included_packages = recipe.includes_set.values_list('pk',
                                                            flat=True)

        # If we're adding back a package which used to be included in this
        # image all we need to do is remove it from the excludes
        if package.pk in included_packages:
            try:
                recipe.excludes_set.remove(package)
                return {"error": "ok"}
            except Package.DoesNotExist:
                return error_response("Package %s not found in excludes"
                                      " but was in included list" %
                                      package.name)

        else:
            recipe.appends_set.add(package)
            # Make sure that package is not in the excludes set
            try:
                recipe.excludes_set.remove(package)
            except:
                pass
            # Add the dependencies we think will be added to the recipe
            # as a result of appending this package.
            # TODO this should recurse down the entire deps tree
            for dep in package.package_dependencies_source.all_depends():
                try:
                    cust_package = CustomImagePackage.objects.get(
                        name=dep.depends_on.name)

                    recipe.includes_set.add(cust_package)
                    try:
                        # When adding the pre-requisite package, make
                        # sure it's not in the excluded list from a
                        # prior removal.
                        recipe.excludes_set.remove(cust_package)
                    except package.DoesNotExist:
                        # Don't care if the package had never been excluded
                        pass
                except:
                    logger.warning("Could not add package's suggested"
                                   "dependencies to the list")
        return JsonResponse({"error": "ok"})

    def delete(self, request, *args, **kwargs):
        recipe, error = XhrCustomRecipeId._get_ci_recipe(kwargs['recipe_id'])
        package, error = self._get_package(kwargs['package_id'])
        if error:
            return error

        try:
            included_packages = recipe.includes_set.values_list('pk',
                                                                flat=True)
            # If we're deleting a package which is included we need to
            # Add it to the excludes list.
            if package.pk in included_packages:
                recipe.excludes_set.add(package)
            else:
                recipe.appends_set.remove(package)
                all_current_packages = recipe.get_all_packages()

                reverse_deps_dictlist = self._get_all_dependents(
                    package.pk,
                    all_current_packages)

                ids = [entry['pk'] for entry in reverse_deps_dictlist]
                reverse_deps = CustomImagePackage.objects.filter(id__in=ids)
                for r in reverse_deps:
                    try:
                        if r.id in included_packages:
                            recipe.excludes_set.add(r)
                        else:
                            recipe.appends_set.remove(r)
                    except:
                        pass

            return JsonResponse({"error": "ok"})
        except CustomImageRecipe.DoesNotExist:
            return error_response("Tried to remove package that wasn't"
                                  " present")
