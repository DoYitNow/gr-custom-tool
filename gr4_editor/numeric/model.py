"""Sequential native resource model; region and ISP behavior remain hypotheses."""
import numpy as np
from .polynomial import fit, predict, log_domain, fit_matrix
from ..codec.gr4_color_matrix_codec import main_coefficients
from ..codec.gr4_gamma_codec import compose_gamma_sources
AXIS=np.linspace(0,1,256)
BANK_NODES=np.array([0,800,1600,2400,3200],dtype=float)/4095
RIDGE=1e-5


def grouping(rgb, kind, native):
    coordinate=rgb@np.array([[77,150,29],[-43,-85,128],[128,-107,-21]],dtype=float).T/256
    cb,cr=coordinate[:,1],coordinate[:,2]
    q=np.where(cr>=0,np.where(cb>=0,0,1),np.where(cb<0,2,3))
    x=np.choose(q,[cb,cr,-cb,-cr]);y=np.choose(q,[cr,-cb,-cr,cb])
    ratio=np.divide(2048*x,x+y,out=np.full(len(rgb),2048.),where=(x+y)>1e-12)
    boundaries=np.asarray(native['0x0']).reshape(4,4)
    region=(ratio[:,None]<boundaries[q]).sum(1)
    group=np.asarray(native['0x4']).reshape(4,5)[q,region]
    scalar=coordinate[:,0] if kind=='luminance' else np.abs(cb)+np.abs(cr)
    position=np.interp(np.clip(scalar,0,1),BANK_NODES,np.arange(5))
    low=np.minimum(position.astype(int),3);fraction=position-low
    weights=np.zeros((len(rgb),5))
    weights[np.arange(len(rgb)),low]=1-fraction
    weights[np.arange(len(rgb)),low+1]=fraction
    return group,weights


def gamma_apply(rgb,curves):
    return np.column_stack([np.interp(np.clip(rgb[:,c],0,1),AXIS,curves[c]) for c in range(3)])


def gamma_inverse(target,curves):
    rows=[]
    for c in range(3):
        values,indices=np.unique(curves[c],return_index=True)
        rows.append(np.interp(target[:,c],values,AXIS[indices]))
    return np.column_stack(rows)


def make_base_model(source,target,base):
    polynomial=fit(log_domain(source),target,features=20)
    neutral=predict(log_domain(np.repeat(AXIS[:,None],3,axis=1)),polynomial).T
    # Monotone envelope is explicit; this is not a recovered Adobe curve.
    neutral=np.maximum.accumulate(np.clip(neutral,0,1),axis=1)
    inverse=np.interp(np.arange(256)*64,base,AXIS)
    gamma_sources=np.rint(np.vstack([np.interp(inverse,AXIS,row) for row in neutral])*16384).astype(int)
    _,composed=compose_gamma_sources(base.tolist(),gamma_sources.tolist())
    gamma=np.array(composed[:3],dtype=float)/16384
    matrix,iterations=fit_matrix(source,target,gamma.T)
    q13=np.rint(matrix*8192).astype(int).reshape(-1).tolist()
    packed=main_coefficients(q13,[8192 if i%4==0 else 0 for i in range(9)],128,256,True)
    effective=np.array(packed).reshape(3,3)/256
    return dict(q13=q13,packed=packed,matrix=effective,gamma=gamma,
        gamma_sources=gamma_sources,polynomial=polynomial,iterations=iterations)


def fit_multi(rgb,target,kind,native,curves):
    desired=gamma_inverse(target,curves)
    groups,weights=grouping(rgb,kind,native)
    matrices=np.tile(np.eye(3),(12,5,1,1))
    support=[]
    for group in range(12):
        selected=groups==group;x=rgb[selected];y=desired[selected];w=weights[selected]
        support.append(dict(group=group,pixels=int(selected.sum()),bank_weight_sums=w.sum(0).tolist()))
        if len(x)<12:continue
        penalty=np.eye(10)*RIDGE
        # Fixed neighboring-bank regularizer, chosen before held-out results.
        for bank in range(4):
            for j in range(2):
                delta=np.zeros(10);delta[bank*2+j]=1;delta[(bank+1)*2+j]=-1
                penalty+=RIDGE*np.outer(delta,delta)
        for c in range(3):
            others=[j for j in range(3) if j!=c]
            design=(w[:,:,None]*(x[:,others]-x[:,c,None])[:,None,:]).reshape(len(x),10)
            parameters=np.linalg.solve(design.T@design+len(x)*penalty,design.T@(y[:,c]-x[:,c])).reshape(5,2)
            parameters=parameters.reshape(10)
            # Optimize the final output loss rather than unweighted inverse
            # Gamma coordinates, which overemphasize flat curve intervals.
            reference=target[selected,c]
            curve=curves[c]
            slopes=np.diff(curve)*255
            def objective(p):
                z=np.clip(x[:,c]+design@p,0,1)
                residual=np.interp(z,AXIS,curve)-reference
                return float(residual@residual+len(x)*(p@penalty@p))
            for _ in range(8):
                raw=x[:,c]+design@parameters;z=np.clip(raw,0,1)
                residual=reference-np.interp(z,AXIS,curve)
                derivative=slopes[np.minimum((z*255).astype(int),254)]
                derivative[(raw<0)|(raw>1)]=0
                jacobian=design*derivative[:,None]
                proposal=np.linalg.solve(jacobian.T@jacobian+len(x)*penalty,
                    jacobian.T@(residual+jacobian@parameters))
                options=[parameters+(proposal-parameters)*factor for factor in (1.,.5,.25,.125,0.)]
                costs=[objective(option) for option in options]
                best=int(np.argmin(costs))
                if best==4:break
                parameters=options[best]
            parameters=parameters.reshape(5,2)
            for bank in range(5):
                row=np.zeros(3);row[others]=parameters[bank];row[c]=-parameters[bank].sum()
                # Contract the complete delta to keep all three coefficients
                # in s12, including the implied diagonal; never wrap a clip.
                scale=1.
                for j in range(3):
                    initial=1. if j==c else 0.
                    if row[j]>0:scale=min(scale,(2047/1024-initial)/row[j])
                    if row[j]<0:scale=min(scale,(-2048/1024-initial)/row[j])
                matrices[group,bank,c]=np.eye(3)[c]+row*scale
    quantized=np.rint(matrices*1024).astype(int)
    for c in range(3):
        others=[j for j in range(3) if j!=c]
        quantized[:,:,c,c]=1024-quantized[:,:,c,others].sum(-1)
    assert quantized.min()>=-2048 and quantized.max()<=2047
    assert np.all(quantized.sum(-1)==1024)
    return quantized,support


def fit_continuous_multi(rgb,target,kind,native,curves):
    initial,support=fit_multi(rgb,target,kind,native,curves)
    groups,weights=grouping(rgb,kind,native)
    conversion=np.array([[77,150,29],[-43,-85,128],[128,-107,-21]],dtype=float)/256
    inverse=np.linalg.inv(conversion)
    angles=set(float(q*90) for q in range(4))
    for q in range(4):
        for value in native['0x0'][q*4:q*4+4]:
            angles.add(float((q*90+np.degrees(np.arctan2(2048-value,value)))%360))
    boundaries=[]
    for angle in sorted(angles):
        theta=np.radians([angle-1e-5,angle+1e-5])
        pair=np.column_stack([np.full(2,.4),.03*np.cos(theta),.03*np.sin(theta)])@inverse.T
        sides,_=grouping(pair,kind,native)
        if sides[0]==sides[1]:continue
        direction=np.array([0,np.cos(np.radians(angle)),np.sin(np.radians(angle))])@inverse.T
        boundaries.append((int(sides[0]),int(sides[1]),direction))
    assert len(boundaries)==12
    matrices=np.tile(np.eye(3),(12,5,1,1))
    for c in range(3):
        others=[j for j in range(3) if j!=c]
        constraints=[]
        for left,right,direction in boundaries:
            basis=direction[others]-direction[c]
            for bank in range(5):
                row=np.zeros(120)
                row[(left*5+bank)*2:(left*5+bank)*2+2]=basis
                row[(right*5+bank)*2:(right*5+bank)*2+2]=-basis
                constraints.append(row)
        constraint=np.asarray(constraints)
        _,singular,right=np.linalg.svd(constraint,full_matrices=True)
        rank=int((singular>1e-10).sum());null=right[rank:].T
        assert np.max(np.abs(constraint@null))<1e-10
        parameters=np.empty((12,5,2))
        for j,other in enumerate(others):parameters[:,:,j]=initial[:,:,c,other]/1024
        parameters=null@(null.T@parameters.reshape(-1))
        blocks=[];penalty=np.zeros((120,120))
        for group in range(12):
            mask=groups==group;x=rgb[mask];w=weights[mask]
            design=(w[:,:,None]*(x[:,others]-x[:,c,None])[:,None,:]).reshape(len(x),10)
            regularizer=np.eye(10)*RIDGE
            for bank in range(4):
                for j in range(2):
                    d=np.zeros(10);d[bank*2+j]=1;d[(bank+1)*2+j]=-1
                    regularizer+=RIDGE*np.outer(d,d)
            penalty[group*10:(group+1)*10,group*10:(group+1)*10]=max(len(x),1)*regularizer
            blocks.append((x[:,c],design,target[mask,c]))
        curve=curves[c];slopes=np.diff(curve)*255
        def objective(p):
            cost=float(p@penalty@p)
            for group,(x,design,reference) in enumerate(blocks):
                z=np.clip(x+design@p[group*10:(group+1)*10],0,1)
                residual=np.interp(z,AXIS,curve)-reference;cost+=float(residual@residual)
            return cost
        for _ in range(8):
            hessian=penalty.copy();rhs=np.zeros(120)
            for group,(x,design,reference) in enumerate(blocks):
                at=slice(group*10,(group+1)*10);p=parameters[at]
                raw=x+design@p;z=np.clip(raw,0,1)
                residual=reference-np.interp(z,AXIS,curve)
                derivative=slopes[np.minimum((z*255).astype(int),254)]
                derivative[(raw<0)|(raw>1)]=0
                jacobian=design*derivative[:,None]
                hessian[at,at]+=jacobian.T@jacobian
                rhs[at]=jacobian.T@(residual+jacobian@p)
            proposal=null@np.linalg.solve(null.T@hessian@null,null.T@rhs)
            options=[parameters+(proposal-parameters)*factor for factor in (1.,.5,.25,.125,0.)]
            costs=[objective(p) for p in options];best=int(np.argmin(costs))
            if best==4:break
            parameters=options[best]
        assert np.max(np.abs(constraint@parameters))<1e-9
        parameters=parameters.reshape(12,5,2)
        for j,other in enumerate(others):matrices[:,:,c,other]=parameters[:,:,j]
        matrices[:,:,c,c]=1-parameters.sum(-1)
    delta=matrices-np.eye(3)
    scale=1.
    for c in range(3):
        for j in range(3):
            initial_value=1. if j==c else 0.
            for value in (float(delta[:,:,c,j].min()),float(delta[:,:,c,j].max())):
                if value>0:scale=min(scale,(2047/1024-initial_value)/value)
                if value<0:scale=min(scale,(-2048/1024-initial_value)/value)
    # One global contraction preserves all inter-region constraints.
    quantized=np.rint((np.eye(3)+delta*scale)*1024).astype(int)
    for c in range(3):
        others=[j for j in range(3) if j!=c]
        quantized[:,:,c,c]=1024-quantized[:,:,c,others].sum(-1)
    assert quantized.min()>=-2048 and quantized.max()<=2047
    assert np.all(quantized.sum(-1)==1024)
    return quantized,support


def multi_apply(rgb,coefficients,kind,native):
    groups,weights=grouping(rgb,kind,native)
    blended=np.einsum('nb,nbij->nij',weights,coefficients[groups]/1024)
    return np.clip(np.einsum('nij,nj->ni',blended,rgb),0,1)


def evaluate(rgb,model,coefficients,kind,native):
    transformed=np.clip(rgb@model['matrix'].T,0,1)
    if coefficients is not None:transformed=multi_apply(transformed,coefficients,kind,native)
    return gamma_apply(transformed,model['gamma'])
