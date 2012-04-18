import calendar
import httplib
import socket
import time
import urllib2

from django.conf import settings

import fudge
from fudge.inspector import arg
import jwt
import mock
from nose.tools import eq_

import amo
from users.models import UserProfile

from mkt.inapp_pay import tasks
from mkt.inapp_pay.models import InappPayNotice
from mkt.inapp_pay.tests.test_views import PaymentTest


class TestNotifyApp(PaymentTest):

    def setUp(self):
        super(TestNotifyApp, self).setUp()
        self.user = UserProfile.objects.get(email='regular@mozilla.com')
        self.contrib = self.make_contrib()
        self.postback = '/postback'
        self.chargeback = '/chargeback'
        self.domain = 'somenonexistantappdomain.com'
        self.app.update(app_domain=self.domain)
        self.inapp_config.update(postback_url=self.postback,
                                 chargeback_url=self.chargeback)
        self.payment = self.make_payment(contrib=self.contrib)

    def url(self, path, protocol='https'):
        return protocol + '://' + self.domain + path

    def do_chargeback(self, reason):
        tasks.chargeback_notify(self.payment.pk, reason)

    def notify(self):
        tasks.payment_notify(self.payment.pk)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_notify_pay(self, urlopen):
        url = self.url(self.postback)
        payload = self.payload(typ='mozilla/payments/pay/postback/v1')

        def req_ok(req):
            dd = jwt.decode(req, verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            jwt.decode(req, self.inapp_config.private_key, verify=True)
            return True

        (urlopen.expects_call().with_args(url, arg.passes_test(req_ok),
                                          timeout=5)
                               .returns_fake()
                               .expects('read')
                               .returns(str(self.contrib.pk))
                               .expects('close'))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.notice, amo.INAPP_NOTICE_PAY)
        eq_(notice.success, True)
        eq_(notice.url, url)
        eq_(notice.payment.pk, self.payment.pk)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_notify_refund_chargeback(self, urlopen):
        url = self.url(self.chargeback)
        payload = self.payload(typ='mozilla/payments/pay/chargeback/v1')

        def req_ok(req):
            dd = jwt.decode(req, verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            eq_(dd['response']['transactionID'], self.contrib.pk)
            eq_(dd['response']['reason'], 'refund')
            jwt.decode(req, self.inapp_config.private_key, verify=True)
            return True

        (urlopen.expects_call().with_args(url, arg.passes_test(req_ok),
                                          timeout=5)
                               .returns_fake()
                               .expects('read')
                               .returns(str(self.contrib.pk))
                               .expects('close'))
        self.do_chargeback('refund')
        notice = InappPayNotice.objects.get()
        eq_(notice.notice, amo.INAPP_NOTICE_CHARGEBACK)
        eq_(notice.success, True)
        eq_(notice.url, url)
        eq_(notice.payment.pk, self.payment.pk)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_notify_reversal_chargeback(self, urlopen):
        url = self.url(self.chargeback)
        payload = self.payload(typ='mozilla/payments/pay/chargeback/v1')

        def req_ok(req):
            dd = jwt.decode(req, verify=False)
            eq_(dd['response']['reason'], 'reversal')
            return True

        (urlopen.expects_call().with_args(url, arg.passes_test(req_ok),
                                          timeout=5)
                               .returns_fake()
                               .expects('read')
                               .returns(str(self.contrib.pk))
                               .expects('close'))
        self.do_chargeback('reversal')
        notice = InappPayNotice.objects.get()
        eq_(notice.notice, amo.INAPP_NOTICE_CHARGEBACK)
        eq_(notice.success, True)

    @mock.patch.object(settings, 'INAPP_REQUIRE_HTTPS', True)
    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_force_https(self, urlopen):
        self.inapp_config.update(is_https=False)
        url = self.url(self.postback, protocol='https')
        (urlopen.expects_call().with_args(url, arg.any(), timeout=arg.any())
                               .returns_fake()
                               .is_a_stub())
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.last_error, '')

    @mock.patch.object(settings, 'INAPP_REQUIRE_HTTPS', False)
    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_configurable_https(self, urlopen):
        self.inapp_config.update(is_https=True)
        url = self.url(self.postback, protocol='https')
        (urlopen.expects_call().with_args(url, arg.any(), timeout=arg.any())
                               .returns_fake()
                               .is_a_stub())
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.last_error, '')

    @mock.patch.object(settings, 'INAPP_REQUIRE_HTTPS', False)
    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_configurable_http(self, urlopen):
        self.inapp_config.update(is_https=False)
        url = self.url(self.postback, protocol='http')
        (urlopen.expects_call().with_args(url, arg.any(), timeout=arg.any())
                               .returns_fake()
                               .is_a_stub())
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.last_error, '')

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_notify_timeout(self, urlopen):
        reason = socket.timeout('too slow')
        urlopen.expects_call().raises(urllib2.URLError(reason))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)
        er = notice.last_error
        assert er.startswith('URLError:'), 'Unexpected: %s' % er

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_http_error(self, urlopen):
        urlopen.expects_call().raises(urllib2.HTTPError('url', 404,
                                                        'Not Found', [], None))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)
        er = notice.last_error
        assert er.startswith('HTTPError:'), 'Unexpected: %s' % er

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_invalid_url_error(self, urlopen):
        (urlopen.expects_call()
                .raises(httplib.InvalidURL("nonnumeric port: ''")))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_bad_socket(self, urlopen):
        (urlopen.expects_call()
                .returns_fake().expects('read').raises(socket.error))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_bad_socket_on_open(self, urlopen):
        urlopen.expects_call().raises(socket.error)
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_bad_status_line(self, urlopen):
        urlopen.expects_call().raises(httplib.BadStatusLine(None))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_invalid_app_response(self, urlopen):
        (urlopen.expects_call().returns_fake()
                               .expects('read')
                               .returns('<not a valid response>')
                               .expects('close'))
        self.notify()
        notice = InappPayNotice.objects.get()
        eq_(notice.success, False)

    @fudge.patch('mkt.inapp_pay.tasks.urlopen')
    def test_signed_app_response(self, urlopen):
        app_payment = self.payload()

        # Ensure that the JWT sent to the app for payment notification
        # includes the same payment data that the app originally sent.
        def is_valid(payload):
            data = jwt.decode(payload, self.inapp_config.private_key,
                              verify=True)
            eq_(data['iss'], settings.INAPP_MARKET_ID)
            eq_(data['aud'], self.inapp_config.public_key)
            eq_(data['typ'], 'mozilla/payments/pay/postback/v1')
            eq_(data['request']['price'], app_payment['request']['price'])
            eq_(data['request']['currency'],
                app_payment['request']['currency'])
            eq_(data['request']['name'], app_payment['request']['name'])
            eq_(data['request']['description'],
                app_payment['request']['description'])
            eq_(data['request']['productdata'],
                app_payment['request']['productdata'])
            eq_(data['response']['transactionID'], self.contrib.pk)
            assert data['iat'] <= calendar.timegm(time.gmtime()) + 60, (
                                'Expected iat to be about now')
            assert data['exp'] > calendar.timegm(time.gmtime()) + 3500, (
                                'Expected exp to be about an hour from now')
            return True

        (urlopen.expects_call().with_args(arg.any(), arg.passes_test(is_valid),
                                          timeout=arg.any())
                               .returns_fake()
                               .expects('read')
                               .returns('<not a valid response>')
                               .expects('close'))
        self.notify()
