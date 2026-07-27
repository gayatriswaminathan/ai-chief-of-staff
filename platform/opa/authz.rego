# AI Chief of Staff — authorization policy (generic rules; per-principal specifics live in data.json)
#
# Input shape:
# {
#   "actor":     "sam@example.com",        # authenticated subject (from JWT)
#   "principal": "principal-001",          # tenant / leader this instance action is for
#   "action":    "send_email",             # what the actor wants to do
#   "resource":  {"type": "communication"} # optional resource context
# }
#
# Decision: {"allow": bool, "allow_basis": string, "tier": string}

package cos.authz

import rego.v1

default allow := false

default tier := "none"

# The principal may do anything within their own tenant.
allow if {
	input.actor == data.principals[input.principal].subject
}

allow_basis := "self: actor is the principal" if {
	input.actor == data.principals[input.principal].subject
}

# A delegate may act when their granted tier for this action is not draft-blocked.
grant := data.principals[input.principal].delegations[input.actor][input.action]

tier := grant.tier if grant

allow if {
	grant.tier != "none"
}

allow_basis := sprintf("delegation: %s granted %s at tier %s", [input.principal, input.action, grant.tier]) if {
	grant
	grant.tier != "none"
}

# Eligibility: which actors can perform a given action for a principal (batch check).
eligible_actors contains actor if {
	some actor, grants in data.principals[input.principal].delegations
	grants[input.action].tier != "none"
}

eligible_actors contains data.principals[input.principal].subject
