"""Current-source desktop localization through the authenticated HTTP boundary."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from modport.desktop_i18n import (language_scope, localize_run_display,
                                 normalize_language, owned_display, request_language, t,
                                 git_reason_display, warning_display)
from modport.desktop_service import DesktopApplication, DesktopServer
from modport.workflow import WORKFLOW_VERSION


class _Supervisor:
    def check(self):
        return True, 'systemd persistent service supervision is available'


class DesktopLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.application = DesktopApplication(Path(self.temporary.name), supervisor=_Supervisor())
        self.server = DesktopServer(('127.0.0.1', 0), self.application, 'a' * 64)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()

    def request(self, locale, *, method='GET', path='/api/bootstrap', body=None, authenticated=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        headers = {'Accept-Language': locale}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + 'a' * 64
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authenticated_concurrent_requests_have_isolated_labels_and_fixed_identity(self):
        languages = ['en', 'zh-CN'] * 4
        with patch('modport.desktop_service.shutil.which', return_value=None):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(self.request, languages))
        for locale, (code, response) in zip(languages, results):
            self.assertEqual(code, 200)
            self.assertEqual(response['workflow_version'], WORKFLOW_VERSION)
            self.assertEqual(response['roles'][0]['id'], 'default')
            self.assertEqual(response['roles'][0]['label'], 'Default tasks' if locale == 'en' else '默认任务')
            checks = {check['id']: check for check in response['environment']['checks']}
            self.assertEqual(checks['supervisor']['detail'], 'systemd persistent service supervision is available' if locale == 'en' else 'systemd 持久服务监督可用')
            self.assertEqual(checks['workspace']['label'], 'Separate instance directories' if locale == 'en' else '独立实例目录')
            self.assertEqual(checks['workspace']['action'], 'prepare_workspace')

    def test_validation_authentication_and_unsupported_language_fallback(self):
        english = self.request('fr-FR', method='POST', path='/api/runs', body={})
        chinese = self.request('zh-Hant', method='POST', path='/api/runs', body={})
        self.assertEqual(english, (400, {'error': 'The project name must contain 1–120 characters.'}))
        self.assertEqual(chinese, (400, {'error': '项目名称须包含 1–120 个字符。'}))
        self.assertEqual(self.request('en', authenticated=False), (401, {'error': 'Application authentication required'}))
        self.assertEqual(self.request('zh-CN', authenticated=False), (401, {'error': '需要应用身份认证'}))
        self.assertEqual(self.request('zh-CN', method='POST', path='/api/model-settings', body={}), (400, {'error': '模型设置字段无效'}))
        self.assertEqual(self.request('en', method='POST', path='/api/local-source', body={'path': 'relative'}), (400, {'error': 'The local source folder must use an absolute path.'}))

    def test_raw_external_errors_and_user_model_data_are_preserved(self):
        def failure(*args):
            raise ValueError('项目名称须包含 1–120 个字符。')
        with patch.object(self.application, 'request', side_effect=failure):
            self.assertEqual(self.request('en'), (400, {'error': '项目名称须包含 1–120 个字符。'}))
        raw = {'project_name': '准备', 'messages': [{'content': '准备', 'reply': 'Testing'}],
               'stages': {'preparation': {'label': 'Preparation', 'state': 'running', 'items': [
                   {'id': 'model-task', 'label': 'source', 'detail': '准备', 'state': 'failed', 'stage_id': 'source', 'label_is_stage': False},
                   {'id': 'host-stage', 'label': 'source', 'detail': 'raw source diagnostics', 'state': 'failed', 'stage_id': 'source', 'label_is_stage': True},
                   {'id': 'future-stage', 'label': 'unknown stage', 'stage_id': 'unknown_stage', 'label_is_stage': True}]}}}
        with language_scope('zh-CN'):
            result = localize_run_display(raw)
        self.assertEqual(result['stages']['preparation']['label'], '准备')
        self.assertEqual(result['messages'], raw['messages'])
        self.assertEqual(result['stages']['preparation']['items'][0], raw['stages']['preparation']['items'][0])
        self.assertEqual(result['stages']['preparation']['items'][1]['label'], '源码读取')
        self.assertEqual(result['stages']['preparation']['items'][1]['detail'], 'raw source diagnostics')
        self.assertEqual(result['stages']['preparation']['items'][2], raw['stages']['preparation']['items'][2])
        self.assertEqual(result['project_name'], raw['project_name'])
        self.assertEqual(raw['stages']['preparation']['label'], 'Preparation')

    def test_interpolated_literals_and_language_negotiation(self):
        for locale in ['zh', 'zh-HK', 'zh_Hant']:
            self.assertEqual(normalize_language(locale), 'zh-CN')
        self.assertEqual(request_language('en;q=.3, zh-CN;q=.9'), 'zh-CN')
        self.assertEqual(request_language('fr-FR, zh-CN;q=.5'), 'en')
        self.assertEqual(request_language('zh;q=0, en'), 'en')
        with language_scope('en'):
            self.assertEqual(owned_display('版本声明文件 目录/{user}.gradle 超出读取上限，请手动确认版本。'),
                             'Version declaration file 目录/{user}.gradle exceeds the read limit. Confirm the versions manually.')
            self.assertEqual(t('无法读取远程仓库：{detail}', detail='原文 {user}'), 'Could not read the remote repository: 原文 {user}')
            self.assertEqual(owned_display('arbitrary 原文 {user}'), 'arbitrary 原文 {user}')

    def test_known_git_reason_exposes_languages_while_raw_errors_remain_raw(self):
        reason = 'Git 工作区包含未提交修改或未跟踪文件；请先保存并提交，或选择复制/直接模式。'
        with language_scope('en'):
            result = git_reason_display({'reason': reason, 'dirty': True, 'can_branch': False})
            self.assertEqual(result['reason_translations']['zh-CN'], reason)
            self.assertEqual(result['reason_translations']['en'], result['reason'])
            self.assertEqual(result['dirty'], True)
            self.assertEqual(result['can_branch'], False)
            raw = {'reason': '无法安全检查 Git 仓库：fatal: 原始工具输出', 'can_branch': False}
            self.assertEqual(git_reason_display(raw), raw)

    def test_multiline_launch_diagnostics_do_not_translate_matching_catalog_text(self):
        raw = 'external diagnostic\n项目名称须包含 1–120 个字符。'
        notice = '持久监督器启动失败：' + raw
        value = {'stages': {}, 'notice': notice, '_desktop_notice_parts': [
            {'kind': 'launch_error', 'text': notice},
            {'kind': 'framework', 'text': '执行已完成；行为验收仍未验证。'}]}
        with language_scope('en'):
            result = localize_run_display(value)
        self.assertEqual(result['notice'], 'The persistent supervisor could not start: ' + raw +
                         '\nExecution is complete; behavior acceptance remains unverified.')
        self.assertNotIn('_desktop_notice_parts', result)
        self.assertEqual(value['notice'], notice)

    def test_inspection_warning_languages_are_available_without_reinspection(self):
        messages = ['未能可靠识别源 Minecraft 版本，请手动填写。', 'raw warning 原文 {literal}']
        with language_scope('en'):
            result = warning_display(messages)
        self.assertEqual(result['warnings'], result['warnings_translations']['en'])
        self.assertEqual(result['warnings_translations']['zh-CN'], messages)
        self.assertEqual(result['warnings_translations']['en'][1], messages[1])
        self.assertEqual(messages[0], '未能可靠识别源 Minecraft 版本，请手动填写。')


if __name__ == '__main__':
    unittest.main()
