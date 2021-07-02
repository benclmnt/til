---
Title: OAuth 2.0
tags: [auth, authz]
Date: 22 May 2021
summary: OAuth 2.0 is an authorization scheme that is widely used today. In this note, we discussed oauth's common flows, its tradeoffs and when to use each flow.
---

### Motivation

To authorize one application to access your data, or use features in another application on your behalf, without giving them your password.

### Terms:

1. **Resource Owner**: You! You are the owner of your identity, your data, and any actions that can be performed with your accounts.
2. **Client**: The application that wants to access data or perform actions on behalf of the Resource Owner.
3. **Authorization Server**: The application that knows the Resource Owner, where the Resource Owner already has an account. Can be first party (same as resource server) or third party like FB/Auth0.
4. **Resource Server**: The Application Programming Interface (API) or service the Client wants to use on behalf of the Resource Owner.
5. **Client ID/Secret**: The way the Authorization Server identify + validates the Client.
6. Tokens:
	1. ID tokens are used to cache user profile information. It should never be used to obtain direct access to APIs or to make authorization decisions. Default lifetime: 10 hours.
	2. Access tokens are used to allow an application to access an API. Default lifetime: 24 hours.

### Authorization Code Flow

Use for: server-side web apps

![Flows - Authorization Code - Authorization sequence diagram](https://images.ctfassets.net/cdy7uua7fh8z/2nbNztohyR7uMcZmnUt0VU/2c017d2a2a2cdd80f097554d33ff72dd/auth-sequence-auth-code.png)

Note:
- In step 2, the client send Client ID, Callback URL, Response Type: `code`, and Scope.
- In step 5, the Authorization Server redirects back to the Client using the Callback URL along with the Authorization Code.
- Authorization Code in step 5 is a short-lived and one-time use temporary code the Client gives the Authorization Server in exchange for an Access Token.

### Implicit Flow

DON'T USE. 

It is basically similar to Authorization Code Flow but directly returning tokens, not authorization code, i.e. by passing Response Type : `token` in step 2.

Learn [more](https://medium.com/oauth-2/why-you-should-stop-using-the-oauth-implicit-grant-2436ced1c926)

### Authorization Code with PKCE Flow

[PKCE](https://datatracker.ietf.org/doc/html/rfc7636) : Proof Key for Code Exchange

Use for: SPAs and native apps

![Flows - Authorization Code with PKCE - Authorization sequence diagram](https://images.ctfassets.net/cdy7uua7fh8z/3pstjSYx3YNSiJQnwKZvm5/33c941faf2e0c434a9ab1f0f3a06e13a/auth-sequence-auth-code-pkce.png)

This is similar to Authorization Code Flow with 3 differences:
1. Before the authorization flow, it first creates the **Code Verifier** (high-entropy cryptographic random string) and a corresponding **Code Challenge** (usually a SHA256 hash of the Code Verifier)
2. In step 3, it sends the Code Challenge along with the authorization request. The Authorization Server will store this Code Challenge.
3. In step 7, instead of sending a fixed Client Secret, it sends the Code Verifier, along with the Authorization Code and Client ID.

### Client Credentials Flow

Used for: Machine to Machine authentication.

![Flows - Client Credentials - Authorization sequence diagram](https://images.ctfassets.net/cdy7uua7fh8z/2waLvaQdM5Fl5ZN5xUrF2F/8c5ddae68ac8dd438cdeb91fe1010fd1/auth-sequence-client-credentials.png)

## OIDC

OAuth 2.0 is designed only for _authorization_, for granting access to data and features from one application to another. [OpenID Connect](https://openid.net/connect/) (OIDC)  is a thin layer on top of OAuth 2.0 that introduces a new type of token: the Identity Token. Encoded within these cryptographically signed tokens in [JWT](https://developer.okta.com/docs/api/resources/oidc#access-token) format, is information about the authenticated user. This opened the door to a new level of interoperability and single sign-on.

When an Authorization Server also supports OIDC, it is sometimes called an _identity provider_, since it _provides_ information about the **Resource Owner** back to the **Client**.

To add OIDC capabilities, add the `openid` to the scope.

## Reference links
- https://developer.okta.com/blog/2019/10/21/illustrated-guide-to-oauth-and-oidc
- https://auth0.com/docs/authorization/which-oauth-2-0-flow-should-i-use
- implicit -> pkce for SPA : https://developer.okta.com/blog/2019/08/22/okta-authjs-pkce