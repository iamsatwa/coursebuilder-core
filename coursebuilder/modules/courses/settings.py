# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Classes supporting updates to basic course settings."""

__author__ = 'Abhinav Khandelwal (abhinavk@google.com)'

import cgi
import copy
import logging
import os
import urllib

from common import crypto
from common import safe_dom
from common import schema_fields
from controllers import utils as controllers_utils
from controllers import sites
from models import courses
from models import models
from models import permissions
from models import roles
from models import transforms
from models import vfs
from modules.courses import messages
from modules.dashboard import dashboard
from modules.dashboard import utils as dashboard_utils
from modules.oeditor import oeditor

# Internal name for the settings top-level Dashboard tab
SETTINGS_TAB_NAME = 'settings'

# Name for the permission for read-only access to all course settings.
VIEW_ALL_SETTINGS_PERMISSION = 'settings_viewer'

# Reference to custom_module registered in modules/courses/courses.py
custom_module = None


class CourseSettingsHandler(object):
    """Course settings handler."""

    EXTRA_CSS_FILES = []
    EXTRA_JS_FILES = []
    ADDITIONAL_DIRS = []

    GROUP_SETTINGS_LISTS = {}

    def __init__(self):
        raise NotImplementedError('Not for instantiation; just a namespace')

    @staticmethod
    def post_course_availability(handler):
        course = handler.get_course()
        settings = course.get_environ(handler.app_context)
        availability = handler.request.get('availability') == 'True'
        settings['course']['now_available'] = availability
        course.save_settings(settings)
        handler.redirect('/dashboard')

    @staticmethod
    def post_course_browsability(handler):
        course = handler.get_course()
        settings = course.get_environ(handler.app_context)
        browsability = handler.request.get('browsability') == 'True'
        settings['course']['browsable'] = browsability
        course.save_settings(settings)
        handler.redirect('/dashboard')

    @staticmethod
    def get_schema_title(name):
        return courses.Course.create_base_settings_schema().\
            get_sub_registry(name).title

    @staticmethod
    def _show_edit_settings_section(
            handler, template_values, key, section_names, exit_url=''):
        # The editor for all course settings is getting rather large.  Here,
        # prune out all sections except the one named.  Names can name either
        # entire sub-registries, or a single item.  E.g., "course" selects all
        # items under the 'course' sub-registry, while
        # "base.before_head_tag_ends" selects just that one field.
        registry = handler.get_course().create_settings_schema()
        registry = registry.clone_only_items_named(section_names)
        permissions.SchemaPermissionRegistry.redact_schema_to_permitted_fields(
            handler.app_context, registry)

        rest_url = handler.canonicalize_url(CourseSettingsRESTHandler.URI)
        form_html = oeditor.ObjectEditor.get_html_for(
            handler, registry.get_json_schema(), registry.get_schema_dict(),
            key, rest_url, exit_url,
            extra_css_files=CourseSettingsHandler.EXTRA_CSS_FILES,
            extra_js_files=CourseSettingsHandler.EXTRA_JS_FILES,
            additional_dirs=CourseSettingsHandler.ADDITIONAL_DIRS,
            required_modules=CourseSettingsRESTHandler.REQUIRED_MODULES)
        template_values.update({
            'main_content': form_html,
        })

    @staticmethod
    def _show_settings_tab(handler, section_names):
        menu_item = dashboard.DashboardHandler.actions_to_menu_items[
            handler.request.get('action') or 'settings_course']
        template_values = {
            'page_title': handler.format_title(
                'Settings > {}'.format(urllib.unquote(menu_item.title))),
        }
        exit_url = handler.request.get('exit_url')

        CourseSettingsHandler._show_edit_settings_section(
            handler, template_values, '/course.yaml', exit_url=exit_url,
            section_names=section_names)
        return template_values

    @classmethod
    def register_settings_section(
        cls, settings, name=None, placement=None, title=None,
        sub_group_name=None):
        """Register a group of settings for a module.

        Args:
          settings: A string or a list of strings that specify paths within
            settings in course.yaml tree.  E.g., 'course' picks the entire
            course subtree; 'course.main_image' that subgroup of settings, and
            'course.main_image.alt_text' just that one item.
          name: Internal short name for menu sub group.  Must
            be globally unique vs. all modules' settings subgroups calling
            this function.  Choose names with lowercase/numbers/underscores;
            e.g., 'units', 'i18n', etc.
          placement: Determines ordering in the menu.  See common/menus.py.
          title: Display name for this settings submenu.
          sub_group_name: see dashboard.
        """
        if isinstance(settings, basestring):
            settings = [settings]
        if name is None:
            name = settings[0]
        if title is None:
            title = cls.get_schema_title(settings[0])
        if sub_group_name is None:
            sub_group_name = 'default'

        action_name = 'settings_%s' % name

        if name in cls.GROUP_SETTINGS_LISTS:
            cls.GROUP_SETTINGS_LISTS[name].extend(settings)
            tab = dashboard.DashboardHandler.root_menu_group.get_child(
                SETTINGS_TAB_NAME).get_child(sub_group_name).get_child(name)
            if tab.title != title:
                logging.warning(
                    'Title %s of settings sub group %s does not match title '
                    '%s from earlier registration.',
                        name, title, tab.title)
            if tab.placement != placement:
                logging.warning(
                    'Placement %d of settings sub group %s does not match '
                    'placement %d from earlier registration.',
                        placement, title,
                        tab.placement)
        else:
            cls.GROUP_SETTINGS_LISTS[name] = copy.copy(settings)
            dashboard.DashboardHandler.add_sub_nav_mapping(
                SETTINGS_TAB_NAME, name, title,
                action=action_name,
                contents=(lambda h: CourseSettingsHandler._show_settings_tab(
                    h, cls.GROUP_SETTINGS_LISTS[name])),
                placement=placement, sub_group_name=sub_group_name)
            dashboard.DashboardHandler.map_get_action_to_permission_checker(
                action_name,
                permissions.SchemaPermissionRegistry.build_view_checker(
                    cls.GROUP_SETTINGS_LISTS[name]))


class CourseYamlRESTHandler(controllers_utils.BaseRESTHandler):
    """Common base for REST handlers in this file."""

    def get_course_dict(self):
        return self.get_course().get_environ(self.app_context)

    def get(self):
        """Handles REST GET verb and returns an object as JSON payload."""
        assert self.app_context.is_editable_fs()

        key = self.request.get('key')

        if not permissions.SchemaPermissionRegistry.can_view(self.app_context):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return

        # Load data if possible.
        fs = self.app_context.fs.impl
        filename = fs.physical_to_logical('/course.yaml')
        try:
            stream = fs.get(filename)
        except:  # pylint: disable=bare-except
            stream = None
        if not stream:
            transforms.send_json_response(
                self, 404, 'Object not found.', {'key': key})
            return

        # Prepare data.
        json_payload = self.process_get()
        transforms.send_json_response(
            self, 200, 'Success.',
            payload_dict=json_payload,
            xsrf_token=crypto.XsrfTokenManager.create_xsrf_token(
                self.XSRF_ACTION))

    def put(self):
        """Handles REST PUT verb with JSON payload."""
        assert self.app_context.is_editable_fs()

        request_param = self.request.get('request')
        if not request_param:
            transforms.send_json_response(
                self, 400, 'Missing "request" parameter.')
            return
        try:
            request = transforms.loads(request_param)
        except ValueError:
            transforms.send_json_response(
                self, 400, 'Malformed "request" parameter.')
            return
        key = request.get('key')
        if not key:
            transforms.send_json_response(
                self, 400, 'Request missing "key" parameter.')
            return
        payload_param = request.get('payload')
        if not payload_param:
            transforms.send_json_response(
                self, 400, 'Request missing "payload" parameter.')
            return
        try:
            payload = transforms.loads(payload_param)
        except ValueError:
            transforms.send_json_response(
                self, 400, 'Malformed "payload" parameter.')
            return
        if not self.assert_xsrf_token_or_fail(
                request, self.XSRF_ACTION, {'key': key}):
            return
        if not permissions.SchemaPermissionRegistry.can_edit(self.app_context):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return

        request_data = self.process_put(request, payload)

        schema = self.get_course().create_settings_schema()
        permissions.SchemaPermissionRegistry.redact_schema_to_permitted_fields(
            self.app_context, schema)
        schema.redact_entity_to_schema(payload)

        if request_data:
            course_settings = courses.deep_dict_merge(
                request_data, self.get_course_dict())
            self.postprocess_put(course_settings, request)

            if not self.get_course().save_settings(course_settings):
                transforms.send_json_response(self, 412, 'Validation error.')
            transforms.send_json_response(self, 200, 'Saved.')

    def postprocess_put(self, course_settings, request):
        pass

    def delete(self):
        """Handles REST DELETE verb with JSON payload."""

        key = self.request.get('key')

        if not self.assert_xsrf_token_or_fail(
                self.request, self.XSRF_ACTION, {'key': key}):
            return

        if (not permissions.SchemaPermissionRegistry.can_edit(self.app_context)
            or not self.is_deletion_allowed()):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return

        entity = self.process_delete()
        if self.get_course().save_settings(entity):
            transforms.send_json_response(self, 200, 'Deleted.')


class CourseSettingsRESTHandler(CourseYamlRESTHandler):
    """Provides REST API for a file."""

    REQUIRED_MODULES = [
        'inputex-date', 'inputex-string', 'inputex-textarea', 'inputex-url',
        'inputex-checkbox', 'inputex-select', 'gcb-uneditable', 'gcb-rte',
        'inputex-hidden', 'inputex-integer',
    ]

    URI = '/rest/course/settings'

    XSRF_ACTION = 'basic-course-settings-put'

    def get_group_id(self, email):
        if not email or '@googlegroups.com' not in email:
            return None
        return email.split('@')[0]

    def get_groups_web_url(self, email):
        group_id = self.get_group_id(email)
        if not group_id:
            return None
        return 'https://groups.google.com/group/' + group_id

    def get_groups_embed_url(self, email):
        group_id = self.get_group_id(email)
        if not group_id:
            return None
        return 'https://groups.google.com/forum/embed/?place=forum/' + group_id

    def process_get(self):
        entity = {}
        schema = self.get_course().create_settings_schema()
        permissions.SchemaPermissionRegistry.redact_schema_to_permitted_fields(
            self.app_context, schema)
        schema.convert_entity_to_json_entity(
            self.get_course_dict(), entity)
        json_payload = transforms.dict_to_json(
            entity, schema.get_json_schema_dict())

        return json_payload

    def _process_course_data(self, course_data):
        if 'forum_email' in course_data:
            forum_email = course_data['forum_email']
            forum_web_url = self.get_groups_web_url(forum_email)
            if forum_web_url:
                course_data['forum_url'] = forum_web_url
            forum_web_url = self.get_groups_embed_url(forum_email)
            if forum_web_url:
                course_data['forum_embed_url'] = forum_web_url

    def _process_extra_locales(self, extra_locales):
        """Make sure each locale has a label to go along."""
        existing = set([
            label.title for label in models.LabelDAO.get_all_of_type(
                models.LabelDTO.LABEL_TYPE_LOCALE)])

        course_locale = self.app_context.default_locale
        for extra_locale in extra_locales + [{'locale': course_locale}]:
            locale = extra_locale['locale']
            if locale in existing:
                continue
            models.LabelDAO.save(models.LabelDTO(
                None, {'title': locale,
                       'version': '1.0',
                       'description': '[%s] locale' % locale,
                       'type': models.LabelDTO.LABEL_TYPE_LOCALE}))

    def process_put(self, request, payload):
        errors = []
        request_data = {}
        schema = self.get_course().create_settings_schema()
        schema.convert_json_to_entity(payload, request_data)
        schema.validate(request_data, errors)

        if errors:
            transforms.send_json_response(
                self, 400, 'Invalid data: \n' + '\n'.join(errors))
            return

        if 'extra_locales' in request_data:
            self._process_extra_locales(request_data['extra_locales'])
        if 'course' in request_data:
            self._process_course_data(request_data['course'])

        return request_data

    def is_deletion_allowed(self):
        return False


class HtmlHookHandler(controllers_utils.ApplicationHandler):
    """Set up for OEditor manipulation of HTML hook contents.

    A separate handler and REST handler is required for hook contents,
    since the set of hooks is not statically known.  Users are free to add
    whatever hooks they want where-ever they want with fairly arbitrary
    names.  This class and its companion REST class deal with persisting the
    hook values into the course.yaml settings.
    """

    @classmethod
    def get_edit_html_hook(cls, handler):
        key = handler.request.get('key')

        registry = HtmlHookRESTHandler.REGISTRY
        exit_url = handler.canonicalize_url(handler.request.referer)
        rest_url = handler.canonicalize_url(HtmlHookRESTHandler.URI)
        delete_url = '%s?%s' % (
            handler.canonicalize_url(HtmlHookRESTHandler.URI),
            urllib.urlencode({
                'key': key,
                'xsrf_token': cgi.escape(
                    handler.create_xsrf_token(
                        HtmlHookRESTHandler.XSRF_ACTION))
            }))
        form_html = oeditor.ObjectEditor.get_html_for(
            handler, registry.get_json_schema(), registry.get_schema_dict(),
            key, rest_url, exit_url,
            delete_url=delete_url, delete_method='delete',
            required_modules=HtmlHookRESTHandler.REQUIRED_MODULES)

        template_values = {}
        template_values['page_title'] = handler.format_title('Edit Hook HTML')
        template_values['main_content'] = form_html
        handler.render_page(template_values)


def _create_hook_registry():
    reg = schema_fields.FieldRegistry('Html Hook', description='Html Hook')
    reg.add_property(schema_fields.SchemaField(
        'hook_content', 'HTML Hook Content', 'html',
        optional=True))
    return reg


class HtmlHookRESTHandler(CourseYamlRESTHandler):
    """REST API for individual HTML hook entries in course.yaml."""

    REGISTRY = _create_hook_registry()
    REQUIRED_MODULES = [
        'inputex-textarea', 'gcb-uneditable', 'gcb-rte', 'inputex-hidden']
    URI = '/rest/course/html_hook'
    XSRF_ACTION = 'html-hook-put'

    def process_get(self):
        html_hook = self.request.get('key')
        item = controllers_utils.HtmlHooks.get_content(
            self.get_course(), html_hook)
        return {'hook_content': item}

    def process_put(self, request, payload):
        request_data = {}
        HtmlHookRESTHandler.REGISTRY.convert_json_to_entity(
            payload, request_data)
        if 'hook_content' not in request_data:
            transforms.send_json_response(
                self, 400, 'Payload missing "hook_content" parameter.')
            return None
        key = request.get('key')
        if not key:
            transforms.send_json_response(
                self, 400, 'Blank or missing "key" parameter.')
            return None

        # Walk from bottom to top of hook element name building up
        # dict-in-dict until we are at outermost level, which is
        # the course_dict we will return.
        course_dict = request_data['hook_content']
        for element in reversed(
            key.split(controllers_utils.HtmlHooks.SEPARATOR)):

            course_dict = {element: course_dict}
        return {controllers_utils.HtmlHooks.HTML_HOOKS: course_dict}

    def postprocess_put(self, course_settings, request):
        # We may have HTML hooks that appear starting from the root of the
        # course config dict hierarchy, rather than within the 'html_hooks'
        # top-level dict.  If so, remove the old version so it does not
        # hang around being confusing.  (Note that we only do this as a
        # post-step after process_put(), so we will only delete old items
        # as they are updated by the admin)
        key = request.get('key')
        if key:
            self._process_delete_internal(course_settings, key)

    def is_deletion_allowed(self):
        return True

    def process_delete(self):
        key = self.request.get('key')
        course_dict = self.get_course_dict()

        # Remove from html_hooks sub-dict
        self._process_delete_internal(
            course_dict.get(controllers_utils.HtmlHooks.HTML_HOOKS, {}), key)

        # Also remove from top-level, just in case we have an old course.
        self._process_delete_internal(course_dict, key)
        return course_dict

    def _process_delete_internal(self, course_dict, key):
        pruned_dict = course_dict
        for element in key.split(controllers_utils.HtmlHooks.SEPARATOR):
            if element in pruned_dict:
                if type(pruned_dict[element]) == dict:
                    pruned_dict = pruned_dict[element]
                else:
                    del pruned_dict[element]
        return course_dict


def _get_about_course(handler):

    # Basic course info.
    template_values = {}
    course_info = []
    course_actions = []
    app_context = handler.app_context

    if not app_context.is_editable_fs():
        course_info.append('The course is read-only.')

    currentCourse = courses.Course(handler)
    course_info.append('Schema Version: %s' % currentCourse.version)
    course_info.append('Context Path: %s' % app_context.get_slug())
    course_info.append('Datastore Namespace: %s' %
                       app_context.get_namespace_name())

    # Course file system.
    fs = app_context.fs.impl
    course_info.append(('File System: %s' % fs.__class__.__name__))
    if fs.__class__ == vfs.LocalReadOnlyFileSystem:
        course_info.append(('Home Folder: %s' % sites.abspath(
            app_context.get_home_folder(), '/')))

    data_info = dashboard_utils.list_files(handler, '/data/')

    sections = [
        {
            'description': messages.ABOUT_THE_COURSE_DESCRIPTION,
            'actions': course_actions,
            'children': course_info},]

    if currentCourse.version == courses.COURSE_MODEL_VERSION_1_2:
        sections.append({
            'title': 'Data Files',
            'description': messages.DATA_FILES_DESCRIPTION,
            'children': data_info})

    template_values['alerts'] = handler.get_alerts()
    template_values['sections'] = sections
    return template_values


def _text_file_to_safe_dom(reader, content_if_empty):
    """Load text file and convert it to safe_dom tree for display."""
    info = []
    if reader:
        lines = reader.read().decode('utf-8')
        for line in lines.split('\n'):
            if not line:
                continue
            pre = safe_dom.Element('pre')
            pre.add_text(line)
            info.append(pre)
    else:
        info.append(content_if_empty)
    return info

def _text_file_to_string(reader, content_if_empty):
    """Load text file and convert it to string for display."""
    if reader:
        return reader.read().decode('utf-8')
    else:
        return content_if_empty

def _get_settings_advanced(handler):
    """Renders course settings view."""
    template_values = {}
    actions = []
    app_context = handler.app_context
    if app_context.is_editable_fs():
        actions.append({
            'id': 'edit_course_yaml',
            'caption': 'Advanced Edit',
            'action': handler.get_action_url(
                'create_or_edit_settings',
                extra_args={
                    'from_action': 'settings_advanced',
                    }),
            'xsrf_token': crypto.XsrfTokenManager.create_xsrf_token(
                'create_or_edit_settings')})

    # course.yaml file content.
    yaml_reader = app_context.fs.open(app_context.get_config_filename())
    yaml_info = _text_file_to_safe_dom(yaml_reader, '< empty file >')
    yaml_reader = app_context.fs.open(app_context.get_config_filename())
    yaml_lines = _text_file_to_string(yaml_reader, '< empty file >')

    # course_template.yaml file contents
    course_template_reader = open(os.path.join(os.path.dirname(
        __file__), '../../course_template.yaml'), 'r')
    course_template_info = _text_file_to_safe_dom(
        course_template_reader, '< empty file >')
    course_template_reader = open(os.path.join(os.path.dirname(
        __file__), '../../course_template.yaml'), 'r')
    course_template_lines = _text_file_to_string(
        course_template_reader, '< empty file >')

    template_values['sections'] = [
        {
            'title': 'Contents of course.yaml file',
            'description': messages.CONTENTS_OF_THE_COURSE_DESCRIPTION,
            'actions': actions,
            'children': yaml_info,
            'code': yaml_lines,
            'mode': 'yaml'
        },
        {
            'title': 'Contents of course_template.yaml file',
            'description': messages.COURSE_TEMPLATE_DESCRIPTION,
            'children': course_template_info,
            'code': course_template_lines,
            'mode': 'yaml'
        }
    ]
    return template_values


class ViewAllSettingsPermission(permissions.AbstractSchemaPermission):
    """Binds readability on all course settings to a custom permission.

    This is an optional extra if an admin wants to give otherwise-limited
    roles visibility on all settings.  Note that this is a lazy option -
    better is to add a new permission and bind it to specific
    readable/writable fields by registering a SimpleSchemaPermission
    instance.
    """

    def get_name(self):
        return VIEW_ALL_SETTINGS_PERMISSION

    def applies_to_current_user(self, application_context):
        return roles.Roles.is_user_allowed(
            application_context, custom_module, VIEW_ALL_SETTINGS_PERMISSION)

    def can_view(self, prop_name=None):
        return True

    def can_edit(self, prop_name=None):
        return False


def get_namespaced_handlers():
    return [
        (CourseSettingsRESTHandler.URI, CourseSettingsRESTHandler),
        (HtmlHookRESTHandler.URI, HtmlHookRESTHandler),
    ]


def on_module_enabled(courses_custom_module, perms):
    global custom_module  # pylint: disable=global-statement
    custom_module = courses_custom_module
    perms.append(roles.Permission(VIEW_ALL_SETTINGS_PERMISSION,
                                  'Can view all course settings'))
    permissions.SchemaPermissionRegistry.add(ViewAllSettingsPermission())


    dashboard.DashboardHandler.add_custom_post_action(
        'course_availability', CourseSettingsHandler.post_course_availability)
    dashboard.DashboardHandler.map_post_action_to_permission_checker(
        'course_availability',
        permissions.SchemaPermissionRegistry.build_edit_checker(
            ['course/course:now_available']))

    dashboard.DashboardHandler.add_custom_post_action(
        'course_browsability', CourseSettingsHandler.post_course_browsability)
    dashboard.DashboardHandler.map_post_action_to_permission_checker(
        'course_browsability',
        permissions.SchemaPermissionRegistry.build_edit_checker(
            ['course/course:browsable']))

    dashboard.DashboardHandler.add_custom_get_action(
        'edit_html_hook', HtmlHookHandler.get_edit_html_hook)

    CourseSettingsHandler.register_settings_section(
        'homepage', placement=1000, sub_group_name='pinned')
    CourseSettingsHandler.register_settings_section(
        'course', placement=2000, sub_group_name='pinned')
    CourseSettingsHandler.register_settings_section(
        'unit', placement=3000, sub_group_name='pinned')
    CourseSettingsHandler.register_settings_section('registration')
    CourseSettingsHandler.register_settings_section('assessment')

    dashboard.DashboardHandler.add_sub_nav_mapping(
        SETTINGS_TAB_NAME, 'advanced', 'Advanced', action='settings_advanced',
        contents=_get_settings_advanced, sub_group_name='advanced')
    dashboard.DashboardHandler.add_sub_nav_mapping(
        'help', 'about', 'Debug info', action='settings_about',
        contents=_get_about_course, sub_group_name='advanced')
