# Domain setup

Inspired by [this](https://sneak.berlin/20201029/stop-emailing-like-a-rube/)

## Domain registration

Find a domain registrar that supports 2FA. I use Namecheap. Buy a domain and
you are good to go.

## DNS hosting for the domain

1. Create a Cloudflare account.
2. Enable 2FA.
3. Add your domain name to Cloudflare.
    - Click on "Add a site" button. Fill in details.
    - Copy the 2 nameservers given to you from Cloudflare, and input it to
      Namecheap. It is under Domain management panel > Domain > Nameservers >
      Custom DNS.

## Deploying a custom subdomain from Netlify

1. Go to Cloudflare dashboard > your domain.
2. Go to the DNS Management panel.
3. Add a CNAME record with name `blog` and target `example.netlify.app`. Set
   TTL to auto and make sure proxy status is set to DNS only.
4. You may want to purge cache to immediately see this.
5. Go to Netlify > Domain Management and add domain alias. If all goes well,
   Netlify will automatically create a HTTPS cert from Let's Encrypt for your
   subdomain.

## Custom domain email setup

Now comes the tricky part of the setup. Jeffery uses a paid ProtonMail account
as his mail server. This is an alternative free setup that I tried.

There are 2 parts to email setup:

1. Receive email addressed to any user on my domain (`*@mydomain.com`).
2. Send email from me@mydomain.com.

I use [Forward Email](https://forwardemail.net) for the first part, and use
gmail's "Send email as" feature for the second part. Forward Email is a free
and open source alternative for email forwarding similar to Mailgun or other
mail hosting services.

### Receiving email

1. Create a Forward Email account.
2. Register your domain using Forward Email.
3. Follow this hand-holding
   [guide](https://forwardemail.net/en/faq#how-do-i-get-started-and-set-up-email-forwarding)
   to setup your email forwarding.
4. Additionally, you might want to add a DMARC record for your domain name by
   following the instructions at
   [https://dmarc.postmarkapp.com/](https://dmarc.postmarkapp.com/).

### Sending email

Follow this hand-holding [guide](https://forwardemail.net/en/faq#how-to-send-mail-as-using-gmail) from Forward Email.

Notes:
  - It is recommended to create a new gmail account with random username as
    your information will be publicly searchable (if you are on the FREE
    plan)
  - When generating gmail's app passwords, fill in a more descriptive text
    input e.g. "me@mydomain.com - send email as". This will help identify
    if you need more than one app passwords (in the future). Also, do not
    ever delete the generated app passwords or your email will fail
    sending.
  - If your email does not end with `@gmail.com`, you need to fill in your
    whole email for the username, instead of just the username portion.


## Setting up your custom domain email in Windows 10 Mail App

Problem: Windows 10 Mail App do not have an option to choose the sender email.
So you will end up sending with your gmail account. 

Solution: Taken from
[here](https://answers.microsoft.com/en-us/windows/forum/apps_windows_10-outlook_mail-winpc/how-do-i-change-the-from-address-in-the-windows-10/8ea65c6c-571d-49b2-9ff5-ba2d6edf0cc1?page=3)

1. In the Windows 10 mail app, go to settings menu > "manage accounts" > "add
   account" > "advanced setup" > "Internet email"
2. Fill out the form, with your alias (`me@mydomain.com`) as email, and
   your gmail username as the "User name" Come back to the password later. For
   the incoming and outgoing servers, enter "smtp.gmail.com." Choose IMAP4 for
   the account type. Enter whatever you like for the account name and "Send
   your messages using this name".
3. For the password, enable [two-step
   verification](https://www.google.com/landing/2step/) if you haven't.
   Generate an [app password](https://myaccount.google.com/apppasswords). Copy
   the generated code, and paste it into the password box back in Windows 10
   mail app. 
4. Click sign in, and there! You can now send from your alias.

