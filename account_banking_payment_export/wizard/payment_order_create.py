# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2009 EduSense BV (<http://www.edusense.nl>).
#              (C) 2011 - 2013 Therp BV (<http://therp.nl>).
#              (C) 2014 - 2015 ACSONE SA/NV (<http://acsone.eu>).
#              (C) 2015 Akretion (<http://www.akretion.com>).
#
#    All other contributions are (C) by their respective contributors
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp import models, fields, api, _


class PaymentOrderCreate(models.TransientModel):
    _inherit = 'payment.order.create'

    journal_ids = fields.Many2many(
        'account.journal', string='Journals Filter')
    partner_ids = fields.Many2many(comodel_name='res.partner',
                                   string='Partners')
    invoice = fields.Boolean(
        string='Linked to an Invoice or Refund')
    date_type = fields.Selection([
        ('due', 'Due Date'),
        ('move', 'Move Date'),
        ], string="Type of Date Filter", required=True)
    duedate = fields.Date(required=False)
    move_date = fields.Date(
        string='Move Date', default=fields.Date.context_today)
    populate_results = fields.Boolean(string="Populate Results Directly")

    @api.model
    def default_get(self, field_list):
        res = super(PaymentOrderCreate, self).default_get(field_list)
        context = self.env.context
        if ('entries' in field_list and context.get('line_ids') and
                context.get('populate_results')):
            res.update({'entries': context['line_ids']})
        assert context.get('active_model') == 'payment.order',\
            'active_model should be payment.order'
        assert context.get('active_id'), 'Missing active_id in context !'
        pay_order = self.env['payment.order'].browse(context['active_id'])
        res.update({
            'journal_ids': pay_order.mode.default_journal_ids.ids or False,
            'invoice': pay_order.mode.default_invoice,
            'date_type': pay_order.mode.default_date_type,
            'populate_results': pay_order.mode.default_populate_results,
            })
        return res

    @api.multi
    def extend_payment_order_domain(self, payment_order, domain):
        self.ensure_one()
        if payment_order.payment_order_type == 'payment':
            # For payables, propose all unreconciled credit lines,
            # including partially reconciled ones.
            # If they are partially reconciled with a supplier refund,
            # the residual will be added to the payment order.
            #
            # For receivables, propose all unreconciled credit lines.
            # (ie customer refunds): they can be refunded with a payment.
            # Do not propose partially reconciled credit lines,
            # as they are deducted from a customer invoice, and
            # will not be refunded with a payment.
            domain += [
                ('credit', '>', 0),
                '|',
                ('account_id.type', '=', 'payable'),
                '&',
                ('account_id.type', '=', 'receivable'),
                ('reconcile_partial_id', '=', False),
            ]

    @api.multi
    def filter_lines(self, lines):
        """ Filter move lines before proposing them for inclusion
            in the payment order.

        This implementation filters out move lines that are already
        included in draft or open payment orders. This prevents the
        user to include the same line in two different open payment
        orders. When the payment order is sent, it is assumed that
        the move will be reconciled soon (or immediately with
        account_banking_payment_transfer), so it will not be
        proposed anymore for payment.

        See also https://github.com/OCA/bank-payment/issues/93.

        :param lines: recordset of move lines
        :returns: list of move line ids
        """
        self.ensure_one()
        payment_lines = self.env['payment.line'].\
            search([('order_id.state', 'in', ('draft', 'open', 'done')),
                    ('move_line_id', 'in', lines.ids)])
        to_exclude = set([l.move_line_id.id for l in payment_lines])
        return [l.id for l in lines if l.id not in to_exclude]

    @api.multi
    def search_entries(self):
        """This method taken from account_payment module.
        We adapt the domain based on the payment_order_type
        """
        line_obj = self.env['account.move.line']
        model_data_obj = self.env['ir.model.data']
        # -- start account_banking_payment --
        payment = self.env['payment.order'].browse(
            self.env.context['active_id'])
        # Search for move line to pay:
        journals = self.journal_ids or self.env['account.journal'].search([])
        domain = [('move_id.state', '=', 'posted'),
                  ('reconcile_id', '=', False),
                  ('company_id', '=', payment.mode.company_id.id),
                  ('journal_id', 'in', journals.ids)]
        if self.partner_ids:
            domain.append(('partner_id', 'in', self.partner_ids.ids))
        if self.date_type == 'due':
            domain += [
                '|',
                ('date_maturity', '<=', self.duedate),
                ('date_maturity', '=', False)]
        elif self.date_type == 'move':
            domain.append(('date', '<=', self.move_date))
        if self.invoice:
            domain.append(('invoice', '!=', False))
        self.extend_payment_order_domain(payment, domain)
        # -- end account_direct_debit --
        lines = line_obj.search(domain)
        context = self.env.context.copy()
        context['line_ids'] = self.filter_lines(lines)
        context['populate_results'] = self.populate_results
        if payment.payment_order_type == 'payment':
            context['display_credit'] = True
            context['display_debit'] = False
        else:
            context['display_credit'] = False
            context['display_debit'] = True
        model_datas = model_data_obj.search(
            [('model', '=', 'ir.ui.view'),
             ('name', '=', 'view_create_payment_order_lines')])
        return {'name': _('Entry Lines'),
                'context': context,
                'view_type': 'form',
                'view_mode': 'form',
                'res_model': 'payment.order.create',
                'views': [(model_datas[0].res_id, 'form')],
                'type': 'ir.actions.act_window',
                'target': 'new',
                }

    @api.multi
    def _prepare_payment_line(self, payment, line):
        """This function is designed to be inherited
        The resulting dict is passed to the create method of payment.line"""
        self.ensure_one()
        _today = fields.Date.context_today(self)
        date_to_pay = False  # no payment date => immediate payment
        if payment.date_prefered == 'due':
            # -- account_banking
            # date_to_pay = line.date_maturity
            date_to_pay = (
                line.date_maturity
                if line.date_maturity and line.date_maturity > _today
                else False)
            # -- end account banking
        elif payment.date_prefered == 'fixed':
            # -- account_banking
            # date_to_pay = payment.date_scheduled
            date_to_pay = (
                payment.date_scheduled
                if payment.date_scheduled and payment.date_scheduled > _today
                else False)
            # -- end account banking
        # -- account_banking
        state = 'normal'
        communication = line.ref or '-'
        if line.invoice:
            if line.invoice.reference_type == 'structured':
                state = 'structured'
                # Fallback to invoice number to keep previous behaviour
                communication = line.invoice.reference or line.invoice.number
            else:
                if line.invoice.type in ('in_invoice', 'in_refund'):
                    communication = (
                        line.invoice.reference or
                        line.invoice.supplier_invoice_number or line.ref)
                else:
                    # Make sure that the communication includes the
                    # customer invoice number (in the case of debit order)
                    communication = line.invoice.number
        # support debit orders when enabled
        if line.debit > 0:
            amount_currency = line.amount_residual_currency * -1
        else:
            amount_currency = line.amount_residual_currency
        if payment.payment_order_type == 'debit':
            amount_currency *= -1
        line2bank = line.line2bank(payment.mode.id)
        # -- end account banking
        res = {'move_line_id': line.id,
               'amount_currency': amount_currency,
               'bank_id': line2bank.get(line.id),
               'order_id': payment.id,
               'partner_id': line.partner_id and line.partner_id.id or False,
               # account banking
               'communication': communication,
               'state': state,
               # end account banking
               'date': date_to_pay,
               'currency': (line.invoice and line.invoice.currency_id.id or
                            line.journal_id.currency.id or
                            line.journal_id.company_id.currency_id.id)}
        return res

    @api.multi
    def create_payment(self):
        """This method is a slightly modified version of the existing method on
        this model in account_payment.
        - pass the payment mode to line2bank()
        - allow invoices to create influence on the payment process: not only
          'Free' references are allowed, but others as well
        - check date_to_pay is not in the past.
        """
        if not self.entries:
            return {'type': 'ir.actions.act_window_close'}
        context = self.env.context
        payment_line_obj = self.env['payment.line']
        payment = self.env['payment.order'].browse(context['active_id'])
        # Populate the current payment with new lines:
        for line in self.entries:
            vals = self._prepare_payment_line(payment, line)
            payment_line_obj.create(vals)
        # Force reload of payment order view as a workaround for lp:1155525
        return {'name': _('Payment Orders'),
                'context': context,
                'view_type': 'form',
                'view_mode': 'form,tree',
                'res_model': 'payment.order',
                'res_id': context['active_id'],
                'type': 'ir.actions.act_window'}
