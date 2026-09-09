
copy_file = rule(
    implementation = lambda ctx: ctx.actions.expand_template(
        template = ctx.file.input,
        output = ctx.outputs.output,
        substitutions = {},
    ),
    attrs = dict(
        input = attr.label(mandatory = True, allow_single_file = True),
        output = attr.output(mandatory = True)
    ),
)
