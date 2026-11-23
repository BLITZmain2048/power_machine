"""Stable, machine-readable codes for RMS input and solver errors."""
def coded_error(error_type, code, message, *args, **kwargs):
    """Build the original exception type with an English message and code."""
    error=error_type(message,*args,**kwargs)
    error.code=code
    error.message=message
    return error
