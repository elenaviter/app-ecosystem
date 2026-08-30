# Examples

Runnable examples are grouped by the product or component they integrate
with. Each group maps the runnable code to the application, package, service,
and documentation that own the demonstrated contracts.

## Connection Hub

[`connection-hub/`](connection-hub/README.md) contains integrations with the
Connection Hub product. These examples may import the `connection-hub` package when
they need its delegated-authority client contracts.

| Example | What it demonstrates |
| --- | --- |
| [Direct-admission protected service](connection-hub/direct-admission-service/README.md) | A service outside KDCube asks Connection Hub for a current delegated-access decision before applying its own domain authorization. |

## Layout

```text
examples/
  <product-or-component>/
    README.md
    <runnable-example>/
```

An example has one owning group. Its group README names every supporting
package and hosted application rather than duplicating the example under each
dependency.
