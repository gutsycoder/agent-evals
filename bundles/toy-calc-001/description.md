# toy-calc-001: `divide()` crashes on division by zero

## Bug

`calc.calc.divide(a, b)` performs `a / b` with no guard against `b == 0`.
Calling `divide(1, 0)` currently raises a raw `ZeroDivisionError`, which
leaks an implementation-level exception to callers instead of a clean,
documented error.

## Expected behavior

When `b == 0`, `divide(a, b)` should raise:

```python
ValueError("division by zero")
```

instead of letting `ZeroDivisionError` propagate. Behavior for `b != 0`
is unchanged (`divide(a, b) == a / b`).

## Fix location

`calc/calc.py`, the `divide` function.
