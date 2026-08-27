<?php
/**
 * Plugin Name: Samsoftpay for WooCommerce
 * Plugin URI:  https://api.samsoftpay.com/docs
 * Description: Accept MTN Mobile Money (and more) via Samsoftpay. Customers pay on Samsoftpay's hosted checkout; orders are confirmed by a signed webhook.
 * Version:     1.0.0
 * Author:      Samsoftpay
 * License:     GPL-2.0+
 * Requires at least: 5.6
 * Requires PHP: 7.4
 * WC requires at least: 5.0
 *
 * Drop-in gateway: no card fields on your site, no PCI scope. The customer is sent
 * to Samsoftpay's hosted checkout (a payment link), pays with Mobile Money, and the
 * order is marked paid when Samsoftpay POSTs a signed `charge.succeeded` webhook —
 * verified with your whsec_ secret. A return-poll is the fallback if the webhook is
 * delayed. Everything talks to https://api.samsoftpay.com (same URL for test/live;
 * the sk_ key prefix picks the mode).
 */

if (!defined('ABSPATH')) { exit; }

define('SAMSOFTPAY_BASE_URL', 'https://api.samsoftpay.com');

add_action('plugins_loaded', 'samsoftpay_init_gateway', 11);

function samsoftpay_init_gateway() {
    if (!class_exists('WC_Payment_Gateway')) { return; }

    class WC_Gateway_Samsoftpay extends WC_Payment_Gateway {

        public function __construct() {
            $this->id                 = 'samsoftpay';
            $this->method_title       = 'Samsoftpay (Mobile Money)';
            $this->method_description = 'Accept MTN Mobile Money via Samsoftpay hosted checkout.';
            $this->has_fields         = false;
            $this->supports           = array('products', 'refunds');

            $this->init_form_fields();
            $this->init_settings();

            $this->title       = $this->get_option('title', 'Mobile Money');
            $this->description  = $this->get_option('description', 'Pay with MTN Mobile Money via Samsoftpay.');
            $this->testmode    = 'yes' === $this->get_option('testmode', 'yes');
            $this->secret_key  = $this->testmode ? $this->get_option('test_secret_key') : $this->get_option('live_secret_key');
            $this->webhook_secret = $this->get_option('webhook_secret');

            add_action('woocommerce_update_options_payment_gateways_' . $this->id, array($this, 'process_admin_options'));
            // Samsoftpay -> us: signed webhook lands at ?wc-api=samsoftpay
            add_action('woocommerce_api_samsoftpay', array($this, 'handle_webhook'));
            // return-from-checkout poll fallback (webhook may be delayed)
            add_action('woocommerce_thankyou_' . $this->id, array($this, 'poll_on_return'));
        }

        public function init_form_fields() {
            $this->form_fields = array(
                'enabled'         => array('title' => 'Enable/Disable', 'type' => 'checkbox',
                                           'label' => 'Enable Samsoftpay', 'default' => 'no'),
                'title'           => array('title' => 'Title', 'type' => 'text', 'default' => 'Mobile Money'),
                'description'     => array('title' => 'Description', 'type' => 'textarea',
                                           'default' => 'Pay with MTN Mobile Money via Samsoftpay.'),
                'testmode'        => array('title' => 'Test mode', 'type' => 'checkbox',
                                           'label' => 'Use the sandbox (sk_test_ key)', 'default' => 'yes'),
                'test_secret_key' => array('title' => 'Test secret key (sk_test_...)', 'type' => 'password', 'default' => ''),
                'live_secret_key' => array('title' => 'Live secret key (sk_live_...)', 'type' => 'password', 'default' => ''),
                'webhook_secret'  => array('title' => 'Webhook signing secret (whsec_...)', 'type' => 'password',
                                           'default' => '',
                                           'description' => 'From Account -> Webhooks. Set your Samsoftpay webhook URL to: '
                                                            . home_url('/?wc-api=samsoftpay')),
            );
        }

        /** Create a Samsoftpay payment link and send the customer to hosted checkout. */
        public function process_payment($order_id) {
            $order = wc_get_order($order_id);
            $amount = (int) round($order->get_total());   // whole UGX

            $resp = $this->api('POST', '/v1/payment-links', array(
                'amount'      => $amount,
                'currency'    => get_woocommerce_currency(),
                'reference'   => 'wc_' . $order->get_id() . '_' . $order->get_order_key(),
                'success_url' => $this->get_return_url($order),
                'cancel_url'  => $order->get_cancel_order_url_raw(),
            ), true /* needs Idempotency-Key */);

            if (is_wp_error($resp) || empty($resp['id'])) {
                $msg = is_wp_error($resp) ? $resp->get_error_message() : 'Could not start payment.';
                wc_add_notice('Payment error: ' . $msg, 'error');
                return array('result' => 'failure');
            }

            $order->update_meta_data('_samsoftpay_link_id', $resp['id']);
            $order->update_status('pending', 'Awaiting Samsoftpay Mobile Money payment.');
            $order->save();

            // Redirect to Samsoftpay hosted checkout for this link.
            return array(
                'result'   => 'success',
                'redirect' => SAMSOFTPAY_BASE_URL . '/pay/' . rawurlencode($resp['id']),
            );
        }

        /** Signed webhook from Samsoftpay: verify HMAC, then complete/​fail the order. */
        public function handle_webhook() {
            $raw = file_get_contents('php://input');
            $sig = isset($_SERVER['HTTP_X_SAMSOFTPAY_SIGNATURE']) ? $_SERVER['HTTP_X_SAMSOFTPAY_SIGNATURE'] : '';
            $expected = hash_hmac('sha256', $raw, $this->webhook_secret);
            if (!hash_equals($expected, $sig)) {
                status_header(400); echo 'invalid signature'; exit;
            }
            $evt = json_decode($raw, true);
            $event = isset($evt['event']) ? $evt['event'] : '';
            $data  = isset($evt['data']) ? $evt['data'] : array();
            $ref   = isset($data['reference']) ? $data['reference'] : (isset($data['merchant_reference']) ? $data['merchant_reference'] : '');

            $order = $this->order_from_reference($ref);
            if ($order && in_array($event, array('charge.succeeded'), true)) {
                if (!$order->is_paid()) {
                    $order->payment_complete(isset($data['id']) ? $data['id'] : '');
                    $order->add_order_note('Samsoftpay: payment confirmed (' . (isset($data['id']) ? $data['id'] : '') . ').');
                }
            } elseif ($order && $event === 'charge.failed') {
                $order->update_status('failed', 'Samsoftpay: payment failed (' . (isset($data['failure_reason']) ? $data['failure_reason'] : '') . ').');
            }
            status_header(200); echo json_encode(array('ok' => true)); exit;   // 2xx stops retries
        }

        /** Fallback when the webhook is delayed: check the charge on return. */
        public function poll_on_return($order_id) {
            $order = wc_get_order($order_id);
            if (!$order || $order->is_paid()) { return; }
            $ref = 'wc_' . $order->get_id() . '_' . $order->get_order_key();
            $resp = $this->api('GET', '/v1/charges?reference=' . rawurlencode($ref) . '&limit=1', null, false);
            if (!is_wp_error($resp) && !empty($resp['data'][0])) {
                $c = $resp['data'][0];
                if (($c['status'] ?? '') === 'succeeded' && !$order->is_paid()) {
                    $order->payment_complete($c['id'] ?? '');
                    $order->add_order_note('Samsoftpay: payment confirmed on return.');
                }
            }
        }

        private function order_from_reference($ref) {
            if (strpos($ref, 'wc_') !== 0) { return null; }
            $parts = explode('_', $ref);   // wc_<id>_<key>
            $id = isset($parts[1]) ? (int) $parts[1] : 0;
            $order = $id ? wc_get_order($id) : null;
            return $order ?: null;
        }

        /** Minimal HTTP client (WP HTTP API) with Samsoftpay auth headers. */
        private function api($method, $path, $body = null, $idempotent = false) {
            $args = array(
                'method'  => $method,
                'timeout' => 20,
                'headers' => array(
                    'Authorization' => 'Bearer ' . $this->secret_key,
                    'Content-Type'  => 'application/json',
                    'X-Timestamp'   => (string) time(),
                ),
            );
            if ($idempotent) {
                $args['headers']['Idempotency-Key'] = wp_generate_uuid4();
            }
            if ($body !== null) { $args['body'] = wp_json_encode($body); }
            $res = wp_remote_request(SAMSOFTPAY_BASE_URL . $path, $args);
            if (is_wp_error($res)) { return $res; }
            $code = wp_remote_retrieve_response_code($res);
            $json = json_decode(wp_remote_retrieve_body($res), true);
            if ($code >= 400) {
                return new WP_Error('samsoftpay', isset($json['error']) ? $json['error'] : ('HTTP ' . $code));
            }
            return $json;
        }
    }

    add_filter('woocommerce_payment_gateways', function ($methods) {
        $methods[] = 'WC_Gateway_Samsoftpay';
        return $methods;
    });
}
